# RTSP-over-UDP test harness (RKE2 on Harvester)

A small, layered harness to validate RTSP/UDP video plumbing **before** chasing
performance. It deliberately separates three questions that people usually
tangle together:

1. **Plumbing** – do raw UDP / multicast packets even cross the path?
2. **RTSP functional** – does RTSP negotiate and does RTP/UDP media flow?
3. **Performance / fragmentation** – at 2K/4K bitrate, what's the loss, jitter,
   and are packets fragmenting or hitting an encapsulation MTU wall?

Everything runs on the macvlan `net1` interface, i.e. the path that **bypasses
Cilium/Calico** for the video flow. `eth0` still carries cluster traffic.

## The path under test

```
 PUBLISHER pod (node X)                      CONSUMER pod (node Y)
 ┌─────────────────────┐                     ┌─────────────────────┐
 │ ffmpeg 2K/4K        │                     │ ffmpeg pull (UDP)   │
 │                     │                     │  + tshark sidecar   │
 │ eth0 ── cluster ────┼···(Cilium/Calico)···┼──── eth0            │   <- NOT used
 │ net1 (macvlan)      │                     │ net1 (macvlan)      │      for video
 └────────┼────────────┘                     └────────┼────────────┘
          │  RTP/UDP (overlay bypass)                  │
          ▼                                            ▲
   node vNIC (eth1)                             node vNIC (eth1)
          │  KubeVirt BRIDGE binding ── NOT masquerade/NAT
          ▼                                            ▲
   Harvester bridge  (Multus + bridge CNI, VLAN net) ──┤  IGMP snooping
          │                                            │  + querier matter
          ▼                                            │  for multicast
   physical NIC ───────────► switch / L2 ──────────────┘

  Phase 1: udp_probe.py rides net1 end-to-end (no RTSP)
  Phase 2: ffmpeg + MediaMTX over the same net1 (RTSP/RTP works)
  Phase 3: tshark on net1 -> fragmentation + RTP loss/jitter
```

Two failure points are called out because they cause "0 packets received":
KubeVirt **masquerade/NAT** at the vNIC, and **multicast snooping with no
querier** on the bridge. Phase 1 is designed to expose both.

## Prerequisites
- Multus installed in the RKE2 guest cluster (separate from Harvester's own Multus).
- A Harvester **VLAN/bridge** VM network (bridge binding, *not* the masquerade
  management network) attached to the RKE2 nodes as a second NIC.
- `whereabouts` IPAM (or edit the NAD for host-local/static).
- **Edit `k8s/02-nad-macvlan.yaml`**: set `master` to the node's bridged NIC and
  the IPAM range to a free range on that L2 subnet. Nothing works until this is right.

## Layout
```
k8s/   01 namespace  02 macvlan NAD  05 probes  10 mediamtx  20 publisher  30 consumer
probe/ udp_probe.py   (dependency-free UDP/multicast probe; runs on netshoot)
```

---

## Phase 1 — plumbing (do this first)
No RTSP. Just prove packets traverse node→bridge→node on `net1`.

```bash
kubectl apply -f k8s/01-namespace.yaml -f k8s/02-nad-macvlan.yaml -f k8s/05-probes.yaml
# confirm each pod got a net1 with an IP on your bridged subnet:
kubectl -n rtsp-test exec probe-a -- ip -4 addr show net1
kubectl -n rtsp-test exec probe-b -- ip -4 addr show net1
# copy the probe in:
kubectl -n rtsp-test cp probe/udp_probe.py probe-a:/tmp/udp_probe.py
kubectl -n rtsp-test cp probe/udp_probe.py probe-b:/tmp/udp_probe.py
```

Unicast reachability (A = probe-a net1 IP, B = probe-b net1 IP):
```bash
kubectl -n rtsp-test exec probe-b -- python3 /tmp/udp_probe.py rx --host <B> --iface-addr <B> --port 5004 --timeout 8 &
kubectl -n rtsp-test exec probe-a -- python3 /tmp/udp_probe.py tx --host <B> --port 5004 --count 2000 --rate 400 --size 1200
```
Expect `received≈2000 lost≈0`. If you get **0 received**, the masquerade/NAT
layer or a missing route is eating it — recheck the VM is on the bridge network.

Multicast reachability (this is the IGMP-snooping/querier acid test):
```bash
kubectl -n rtsp-test exec probe-b -- python3 /tmp/udp_probe.py rx --multicast --group 239.255.0.1 --port 5004 --iface-addr <B> --timeout 8 &
kubectl -n rtsp-test exec probe-a -- python3 /tmp/udp_probe.py tx --multicast --group 239.255.0.1 --port 5004 --iface-addr <A> --count 2000 --rate 400 --size 1200
```
**0 received here, with unicast working = classic multicast snooping problem:**
the bridge has `multicast_snooping=1` and no querier, so it drops the group.
Fix on the path (querier on the L2, or disable snooping on the bridge).

### Fragmentation / encapsulation pre-check (still Phase 1)
Find the real end-to-end MTU with the Don't-Fragment flag. Walk the size down
until packets arrive:
```bash
kubectl -n rtsp-test exec probe-a -- python3 /tmp/udp_probe.py tx --host <B> --df --size 1500 --count 5 --rate 5
kubectl -n rtsp-test exec probe-a -- python3 /tmp/udp_probe.py tx --host <B> --df --size 1472 --count 5 --rate 5
```
If 1472 (1500-byte frame) gets through but ~1450 is the ceiling, something is
**encapsulating** your "bypass" path (a stray VXLAN ~50B). On a true macvlan
path you should clear a full 1500 (or your jumbo MTU). This is the cheapest way
to catch the encap problem before it shows up as mangled 4K I-frames.

---

## Phase 2 — RTSP functional
```bash
kubectl apply -f k8s/10-mediamtx.yaml -f k8s/20-publisher.yaml -f k8s/30-consumer.yaml
kubectl -n rtsp-test logs deploy/publisher        # should show ffmpeg pushing frames
kubectl -n rtsp-test logs -f consumer -c ffmpeg   # should show steady bitrate/fps, no errors
```
- `-rtsp_transport udp` forces unicast UDP. To test **multicast**, add
  `multicast` to `rtspTransports` in the MediaMTX configmap and change the
  consumer flag to `-rtsp_transport udp_multicast`.
- Real camera: in `10-mediamtx.yaml` set the path `source:` to the camera RTSP
  URL (Option B) and skip the publisher.

---

## Phase 3 — performance + fragmentation (lower priority)
Bump the publisher to real load (`RES=3840x2160`, `BR=35M`) then capture on
`net1` from the consumer's sidecar:
```bash
kubectl -n rtsp-test exec consumer -c capture -- \
  timeout 20 tcpdump -ni net1 -w /tmp/cap.pcap udp
kubectl -n rtsp-test exec consumer -c capture -- \
  tshark -r /tmp/cap.pcap -q -z io,stat,1 -Y "ip.flags.mf==1 || ip.frag_offset>0"
```
Key things to read off:
- **`ip.flags.mf==1` / `frag_offset>0`** present → IP fragmentation is
  happening (MTU too small for the datagrams, or encoder emitting oversize
  packets). On a correct H.264/FU-A path this should be ~empty.
- **RTP loss/jitter**: `tshark -r /tmp/cap.pcap -q -z rtp,streams` (sequence
  gaps = loss; jitter column = network jitter).
- ffmpeg consumer log: repeated "missed/RTP" warnings = real drops.

## Cleanup
```bash
kubectl delete ns rtsp-test
```

## Debugging gotchas baked into the design
- **macvlan host isolation**: a pod on macvlan-bridge mode can reach other
  hosts but *not its own node* over `net1`. Don't debug by pinging the local
  node — use the peer pod.
- **Different nodes**: probes/consumer use anti-affinity so traffic actually
  crosses the wire; if they land together you're only testing loopback.
- **NetworkPolicy doesn't apply to net1** — don't expect Cilium/Calico policy
  to gate the streaming path.
- **Image tags/arch**: verify `bluenviron/mediamtx` tags exist for your CPU
  arch; swap the ffmpeg image (e.g. `jrottenberg/ffmpeg`) if you prefer.
# rtsp-plumber
