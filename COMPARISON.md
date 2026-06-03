# Networking approaches: macvlan vs native Cilium vs unicast relay vs SR-IOV

Context for the verdicts: **RKE2 on Harvester VMs, 2K/4K RTSP video, goal of
avoiding encapsulation / IP fragmentation.** The right choice hinges on one
number we still need: **how many consumers read a single camera stream.**

## At a glance

| Approach | Multicast | Unicast UDP / RTSP | Encap / frag risk | Perf @ 2K/4K | Setup complexity | NetworkPolicy | Harvester live-migration | Maturity |
|---|---|---|---|---|---|---|---|---|
| **Multus + macvlan (extra NIC)** | ✅ native, real IGMP | ✅ | ✅ none — bypasses overlay | High (near line-rate) | High — NAD, IPAM, in-pod routing, 2nd VM NIC | ❌ not enforced on `net1` | ⚠️ bridge binding complicates it | Mature (macvlan/Multus old & stable) |
| **Native Cilium eBPF multicast** | ⚠️ yes but **requires VXLAN mode** | ✅ | ❌ VXLAN = encap + ~50B MTU loss → the frag you're avoiding | Lower (encap + eBPF processing) | Medium — but manual subscriber mgmt in OSS | ✅ full Cilium policy | ✅ fine (overlay, no special binding) | ⚠️ Beta; enterprise build more complete |
| **Plain CNI + unicast RTSP relay** | ❌ none (relay fans out copies) | ✅ | ⚠️ overlay encap, but fixable: set RTP packet size | Fine for few consumers; bandwidth ×N viewers | ✅ Lowest — no Multus, no 2nd NIC | ✅ full policy | ✅ fine | ✅ Rock-solid |
| **Multus + SR-IOV** | ✅ native, best perf | ✅ | ✅ none | Highest (HW offload) | ❌ impractical inside a Harvester VM (VF passthrough/nesting) | ❌ not on the SR-IOV iface | ❌ generally breaks migration | Mature on bare metal, not the VM case |

## Detail

### Multus + macvlan (extra NIC)
- **Pro:** the only design that gives real multicast *and* no encapsulation, at
  high throughput. Video rides `net1` straight to the L2 segment.
- **Con:** most moving parts (second interface, separate IPAM, app must bind the
  correct interface for multicast), NetworkPolicy gap on the streaming path, and
  it depends on Harvester **bridge binding**, which complicates VM live-migration.
- **Use when:** you genuinely need **many-receiver multicast** without VXLAN overhead.

### Native Cilium eBPF multicast
- **Pro:** everything stays on one interface, full NetworkPolicy, no migration
  caveats, simplest topology.
- **Con:** mandates **VXLAN mode**, reintroducing exactly the encapsulation and
  MTU-shrink/fragmentation you set out to avoid. Still beta, with manual group/
  subscriber management in the OSS build.
- **Use when:** you can tolerate VXLAN overhead and want a single-interface,
  policy-governed design. It's the "simple" answer that trades away the no-encap goal.

### Plain CNI + unicast RTSP relay (MediaMTX)
- **Pro:** by far the simplest — drop Multus entirely, any CNI works, full
  policy, no special bindings. RTSP dynamic ports solved by pinning RTP/RTCP at
  the relay.
- **Con:** no multicast, so the relay sends **one copy per consumer**; bandwidth
  scales linearly with viewers.
- **Use when:** **fan-out per stream is small.** This is the recommended default
  unless the consumer count says otherwise.

### Multus + SR-IOV
- Gold standard on bare metal (hardware offload, line rate, native multicast),
  but inside a Harvester VM it needs VF passthrough into the guest and is rarely
  worth the pain. Listed for completeness.

## Bottom line

There is no option that is simultaneously **simple, encapsulation-free, and
multicast.** Pick the corner you can give up:

- **Few viewers per stream** → give up multicast → *plain CNI + unicast relay* (simplest).
- **Many viewers per stream** → give up simplicity → *Multus + macvlan*.
- **Can tolerate VXLAN** → give up the no-encap goal → *native Cilium multicast*.

The per-stream consumer count resolves the whole table.

> On the fragmentation point that applies to *every* overlay option: the VXLAN
> header shrinks effective MTU, so set ffmpeg's RTP packet size accordingly
> (the H.264 FU-A packetizer respects a max size). Done right, even unicast RTP
> over the overlay carries 2K/4K without IP fragmentation.
