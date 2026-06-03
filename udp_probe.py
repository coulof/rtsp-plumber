#!/usr/bin/env python3
"""
udp_probe.py - dependency-free UDP / multicast plumbing probe.

Phase 1 tool: prove that UDP datagrams (and multicast group joins) actually
traverse the path  pod -> net1(macvlan) -> node vNIC -> Harvester bridge ->
physical  BEFORE introducing any RTSP complexity.

It runs on plain python3 (e.g. the nicolaka/netshoot image), no pip installs.

Why the knobs matter for your case:
  --size   : datagram payload size. Set it ABOVE the path MTU on purpose to
             observe IP fragmentation (and, with --df, blackholing).
  --df     : set Don't-Fragment (IP_PMTUDISC_DO). With DF, an oversized
             datagram is dropped instead of fragmented -> this is how you
             discover the REAL end-to-end MTU and catch a stray VXLAN
             encapsulation eating ~50 bytes.
  --iface-addr : source/join address = the macvlan (net1) IP, so traffic is
             pinned to the overlay-bypass path and not eth0.

Examples
  Receiver (multicast), join group on net1:
    ./udp_probe.py rx --multicast --group 239.255.0.1 --port 5004 \
        --iface-addr 10.10.0.21
  Sender (multicast), send out net1, 1200B payloads, 200 pkt/s, 2000 pkts:
    ./udp_probe.py tx --multicast --group 239.255.0.1 --port 5004 \
        --iface-addr 10.10.0.20 --size 1200 --rate 200 --count 2000

  Unicast variant: drop --multicast/--group and use --host <peer net1 IP>.
  MTU probe: add --df --size 1500 (then 1473, etc.) until packets arrive.
"""
import argparse
import socket
import struct
import sys
import time

MAGIC = b"PROBE"  # 5 bytes, followed by 8-byte big-endian sequence number


def build_packet(seq, size):
    header = MAGIC + struct.pack("!Q", seq)
    pad = size - len(header)
    if pad < 0:
        raise SystemExit(f"--size must be >= {len(header)} bytes")
    return header + (b"\x00" * pad)


def parse_seq(data):
    if len(data) < len(MAGIC) + 8 or data[: len(MAGIC)] != MAGIC:
        return None
    return struct.unpack("!Q", data[len(MAGIC) : len(MAGIC) + 8])[0]


def make_socket(args, sending):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if args.df:
        # Linux: force Don't-Fragment so oversize datagrams are dropped, not split.
        IP_MTU_DISCOVER, IP_PMTUDISC_DO = 10, 2
        s.setsockopt(socket.IPPROTO_IP, IP_MTU_DISCOVER, IP_PMTUDISC_DO)
    if sending and args.multicast:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, args.ttl)
        if args.iface_addr:
            # Pin egress to the macvlan interface (overlay bypass).
            s.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(args.iface_addr),
            )
    return s


def run_tx(args):
    s = make_socket(args, sending=True)
    dest = (args.group if args.multicast else args.host, args.port)
    if not args.multicast and not args.host:
        raise SystemExit("unicast tx needs --host <peer net1 IP>")
    interval = 1.0 / args.rate if args.rate > 0 else 0.0
    sent = 0
    print(f"tx -> {dest}  size={args.size}B  count={args.count}  df={args.df}", flush=True)
    for seq in range(args.count):
        try:
            s.sendto(build_packet(seq, args.size), dest)
            sent += 1
        except OSError as e:
            # With --df this fires when the datagram exceeds path MTU.
            print(f"  seq={seq} send failed: {e} (likely > path MTU)", flush=True)
        if interval:
            time.sleep(interval)
    print(f"done. sent={sent}/{args.count}", flush=True)


def run_rx(args):
    s = make_socket(args, sending=False)
    s.bind(("" if args.multicast else (args.iface_addr or ""), args.port))
    if args.multicast:
        join_if = socket.inet_aton(args.iface_addr) if args.iface_addr else socket.inet_aton("0.0.0.0")
        mreq = socket.inet_aton(args.group) + join_if
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        print(f"joined {args.group} on {args.iface_addr or 'default'}", flush=True)
    s.settimeout(args.timeout)
    got = 0
    first = last = None
    losses = reorder = 0
    print(f"rx :{args.port}  timeout={args.timeout}s ... waiting", flush=True)
    while True:
        try:
            data, addr = s.recvfrom(65535)
        except socket.timeout:
            break
        seq = parse_seq(data)
        if seq is None:
            continue
        got += 1
        if first is None:
            first = seq
            print(f"  first packet from {addr[0]} seq={seq} len={len(data)}", flush=True)
        elif last is not None:
            if seq == last + 1:
                pass
            elif seq > last + 1:
                losses += seq - last - 1
            else:
                reorder += 1
        last = seq
    if got == 0:
        print("RESULT: received 0 packets -> path is NOT carrying this traffic.", flush=True)
        print("  multicast: check IGMP snooping/querier on the bridge + KubeVirt binding (NAT?).", flush=True)
        sys.exit(2)
    span = (last - first + 1) if first is not None else 0
    print(f"RESULT: received={got} span={span} lost={losses} reordered={reorder}", flush=True)


def main():
    p = argparse.ArgumentParser(description="UDP/multicast plumbing probe")
    p.add_argument("mode", choices=["tx", "rx"])
    p.add_argument("--multicast", action="store_true")
    p.add_argument("--group", default="239.255.0.1", help="multicast group")
    p.add_argument("--host", default=None, help="unicast destination IP (tx)")
    p.add_argument("--port", type=int, default=5004)
    p.add_argument("--iface-addr", default=None, help="net1 IP: egress/join interface")
    p.add_argument("--size", type=int, default=1200, help="datagram payload bytes")
    p.add_argument("--count", type=int, default=1000)
    p.add_argument("--rate", type=float, default=200.0, help="packets/sec (0 = max)")
    p.add_argument("--ttl", type=int, default=8)
    p.add_argument("--df", action="store_true", help="set Don't-Fragment")
    p.add_argument("--timeout", type=float, default=5.0, help="rx idle timeout")
    args = p.parse_args()
    (run_tx if args.mode == "tx" else run_rx)(args)


if __name__ == "__main__":
    main()
