# Networking approaches: macvlan vs native Cilium vs plain CNI vs SR-IOV

**Scope:** you own the **infrastructure only** — Harvester, RKE2, the CNI,
interfaces, hardware. You do **not** control the workloads: you can't deploy a
relay or VMS into the cluster, so the **transport (unicast vs multicast) is
decided by the cameras + the consuming application, not by you.** Your job is to
provision a substrate that carries whatever they negotiate and to surface the
constraints they own.

**Numbers (resolved):** 2K/4K RTSP; ~10 clients each viewing 5–10 cameras, out
of **10 cameras in dev / 300 in prod**. Fan-out per camera is low, so multicast
is **not needed for bandwidth** — the only thing that would force it is an
application/camera requirement. Goal throughout: avoid encapsulation / IP
fragmentation.

## At a glance

| Approach | Multicast | Unicast UDP / RTSP | Encap / frag risk | Perf @ 2K/4K | Setup complexity | NetworkPolicy | Harvester live-migration | Maturity |
|---|---|---|---|---|---|---|---|---|
| **Plain CNI (unicast, no relay)** | ❌ none | ✅ | ⚠️ overlay encap, fixable: RTP packet size | Fine; bandwidth ×N viewers per camera | ✅ Lowest — no Multus, no 2nd NIC | ✅ full policy | ✅ fine (masquerade) | ✅ Rock-solid |
| **Multus + macvlan (extra NIC)** | ✅ native, real IGMP | ✅ | ✅ none — bypasses overlay | High (near line-rate) | High — NAD, IPAM, in-pod routing, 2nd VM NIC | ❌ not enforced on `net1` | ⚠️ bridge-bound VM NIC complicates it | Mature (macvlan/Multus old & stable) |
| **Native Cilium eBPF multicast** | ⚠️ yes but **requires VXLAN mode** | ✅ | ❌ VXLAN = encap + ~50B MTU loss → the frag you're avoiding | Lower (encap + eBPF processing) | Medium — but manual subscriber mgmt in OSS | ✅ full Cilium policy | ✅ fine (overlay, no special binding) | ⚠️ Beta; enterprise build more complete |
| **Multus + SR-IOV** | ✅ native, best perf | ✅ | ✅ none | Highest (HW offload) | ❌ impractical inside a Harvester VM (VF passthrough/nesting) | ❌ not on the SR-IOV iface | ❌ generally breaks migration | Mature on bare metal, not the VM case |

## Detail

### Plain CNI (unicast, no relay)
- **Pro:** simplest substrate — no Multus, any CNI works (keep RKE2's default),
  full NetworkPolicy, migration-friendly. Carries whatever unicast RTSP/RTP the
  cameras and application negotiate.
- **Con:** with nothing de-duplicating pulls, each viewer opens its own session
  to the camera — bandwidth scales ×N viewers, and you're exposed to the
  **camera's concurrent-session cap** (see constraints below). Neither is fixable
  in the infrastructure.
- **Use when:** fan-out per camera is small (your case) and the de-dup/fan-out
  problem is owned by an application layer you don't run.

### Multus + macvlan (extra NIC)
- **Pro:** the only design that gives real multicast *and* no encapsulation, at
  high throughput. Video rides `net1` straight to the L2 segment.
- **Con:** most moving parts (second interface, separate IPAM, app must bind the
  correct interface for multicast), NetworkPolicy gap on the streaming path, and
  it depends on Harvester **bridge binding**, which complicates VM live-migration.
- **Use when:** the application/cameras **require multicast** — the only trigger,
  since your bandwidth doesn't demand it.

### Native Cilium eBPF multicast
- **Pro:** everything stays on one interface, full NetworkPolicy, no migration
  caveats, simplest topology.
- **Con:** mandates **VXLAN mode**, reintroducing exactly the encapsulation and
  MTU-shrink/fragmentation you set out to avoid. Still beta, with manual group/
  subscriber management in the OSS build.
- **Use when:** you can tolerate VXLAN overhead and want a single-interface,
  policy-governed design. The "simple" answer that trades away the no-encap goal.

### Multus + SR-IOV
- Gold standard on bare metal (hardware offload, line rate, native multicast),
  but inside a Harvester VM it needs VF passthrough into the guest and is rarely
  worth the pain. Listed for completeness.

## How this affects KubeVirt / Harvester live migration

The key insight: **KubeVirt does not see your in-guest CNI.** It sees the
*Harvester VM's* NICs and their binding types. So "does approach X break live
migration?" really means "does approach X force the RKE2-node VM to carry a
migration-hostile NIC?" Three binding situations matter:

- **Masquerade binding** (Harvester management network): VM sits behind NAT, its
  identity moves with the pod → **live migration supported.**
- **Bridge binding** (Harvester VLAN network): VM's MAC is placed directly on a
  host bridge tied to that node's L2 → **historically blocks live migration.**
  Newer KubeVirt adds a bridge-binding *migration sidecar/binding plugin* that
  can restore it — verify it's enabled and supported on your Harvester version
  before relying on it.
- **PCI passthrough** (SR-IOV VF handed into the VM): a passed-through device
  cannot be migrated → **breaks live migration** outright.

Mapped onto the approaches:

| Approach | NIC the VM must carry for streaming | Live-migration effect |
|---|---|---|
| **Plain CNI (unicast)** | None extra | Clean — node VMs keep a single masquerade NIC. Most migration-friendly. |
| **Multus + macvlan** | A **bridge-bound VLAN NIC** on the VM (so the guest can reach the streaming L2) | Constrained — that bridge-bound NIC is the blocker. OK only with the bridge-binding migration plugin. |
| **Native Cilium multicast** | None extra — video stays on the primary network | Clean *if* the primary network is masquerade. A VLAN (bridge) primary network carries the same constraint regardless of approach. |
| **Multus + SR-IOV** | A **passed-through VF** | Broken — passthrough is incompatible with migration. |

- The macvlan design's real migration cost is **not the macvlan** (that's inside
  the guest) — it's the bridge-bound VLAN NIC the RKE2-node VM needs to reach the
  streaming L2 at all.
- Plain CNI keeps node VMs on one migration-friendly interface — the strongest
  reason to prefer it when migration matters.
- SR-IOV and live migration are mutually exclusive without device detach.

> Note: this is about migrating the *Harvester VMs that host RKE2 nodes*.
> Rescheduling *pods* inside the guest is unaffected — but a pod's macvlan `net1`
> IP is not preserved across a pod reschedule, so don't lean on pod movement for
> streaming-session continuity either.

## Bottom line

There is no option that is simultaneously **simple, encapsulation-free, and
multicast.** Pick the corner you can give up:

- **Low fan-out per camera (your case)** → don't need multicast → *plain CNI, unicast* (simplest).
- **Application requires multicast** → give up simplicity/migration → *Multus + macvlan*.
- **Can tolerate VXLAN** → give up the no-encap goal → *native Cilium multicast*.

Fan-out per camera resolves the table — and yours is low.

> On the fragmentation point that applies to *every* overlay option: the VXLAN
> header shrinks effective MTU, so set the sender's RTP packet size accordingly
> (the H.264 FU-A packetizer respects a max size). Done right, even unicast RTP
> over the overlay carries 2K/4K without IP fragmentation.

## Recommendation — what to provision (infrastructure only)

You provision the substrate; you don't choose the transport. So provision the
**default** and keep the **conditional** in your back pocket.

**Default substrate — build this unless told otherwise:**
- **Plain CNI** — keep RKE2's default (Canal) or Cilium; choose on cluster-traffic
  merits, not multicast. **No Multus, no macvlan, no streaming VLAN.**
- Keep RKE2-node VM NICs on the **masquerade** management network → live
  migration works.
- **MTU discipline:** match the CNI pod MTU to the VM/K8s network; if the overlay
  uses VXLAN, account for ~50 B so unicast RTP doesn't fragment.

This carries unicast RTSP/RTP — the expected case at your fan-out — with the
simplest, most migration-friendly footprint.

**Conditional substrate — only if the cameras/application require MULTICAST:**
- Add **Multus + a macvlan NAD + a bridged streaming VLAN** (the design in this repo).
- Accept the bridge-bound VM NIC and its live-migration trade-off.
- It's an infra build you own, but it's **triggered by an app-layer requirement
  you must confirm first.** Don't pre-build it on spec.

**NIC layout by scale:**
- **Dev (10 cameras):** the **1G NIC is plenty** — a single port covers ingest +
  client egress; LACP-bond two ports to erase the worst-case doubt. 25G stays on
  storage/iSCSI. (Validate LACP per-flow hashing here — a single big flow pins to
  one link regardless of total bond capacity.)
- **Prod (300 cameras):** continuous ingest is ~3–10 Gbps and must move to the
  **25G** NICs alongside storage; 1G drops to host/management. Recording write
  load (~375 MB/s–1.25 GB/s) becomes the dominant sizing problem, not the CNI.

**Constraints you do NOT control — flag these to the camera/application owners:**
- **Camera session limits** (~2–8 concurrent RTSP). With no de-duplicating layer,
  recording + several direct viewers can exhaust a popular camera's cap and get
  clients refused. The infrastructure cannot fix this — raise it.
- **Transport choice is theirs.** If they choose multicast, that triggers your
  conditional substrate above.

> Note: a relay / VMS (e.g. MediaMTX, or an NVR like Milestone / Nx Witness /
> Frigate) would pull each camera once and fan out to recorder + viewers —
> solving the session-limit problem and keeping everything unicast. It's the
> cleanest answer, but it's an **application component outside your control**;
> flag it to whoever owns that layer rather than planning to run it yourself.
