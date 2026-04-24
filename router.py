import socket
import json
import threading
import time
import os
import subprocess
import ipaddress

MY_IP = os.getenv("MY_IP", "127.0.0.1")
NEIGHBORS = [n.strip() for n in os.getenv("NEIGHBORS", "").split(",") if n.strip()]
PORT = 5000
INFINITY = 16
BROADCAST_INTERVAL = 3   # seconds between update broadcasts
NEIGHBOR_TIMEOUT = 9     # 3x broadcast interval — expire silent neighbors

# {subnet_cidr: {"distance": int, "next_hop": str, "learned_from": str|None}}
routing_table = {}
table_lock = threading.Lock()

# subnets directly connected to our own interfaces — never overwrite these via DV
local_subnets = set()

# {neighbor_ip: last_seen_timestamp}
neighbor_last_seen = {}


def get_local_subnets():
    """Return {subnet_cidr: {distance, next_hop, learned_from, ifname}} for all non-lo interfaces."""
    result = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True)
    subnets = {}
    for iface in json.loads(result.stdout):
        if iface.get("ifname") == "lo":
            continue
        ifname = iface["ifname"]
        for addr in iface.get("addr_info", []):
            if addr.get("family") == "inet":
                net = ipaddress.IPv4Network(
                    f"{addr['local']}/{addr['prefixlen']}", strict=False
                )
                subnets[str(net)] = {
                    "distance": 0,
                    "next_hop": "0.0.0.0",
                    "learned_from": None,
                    "ifname": ifname,
                }
    return subnets


def broadcast_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    while True:
        detected = get_local_subnets()

        with table_lock:
            for subnet, info in detected.items():
                local_subnets.add(subnet)
                curr = routing_table.get(subnet)

                if curr is None:
                    # New interface came up after startup
                    routing_table[subnet] = {k: v for k, v in info.items() if k != "ifname"}
                    print(f"[LOCAL] detected new interface subnet: {subnet}")
                    print_table()

                elif curr["learned_from"] is not None:
                    # We previously accepted a DV update for our own subnet.
                    # This corrupted the kernel connected route. Fix it now.
                    old_hop = curr["next_hop"]
                    os.system(f"ip route del {subnet} via {old_hop} 2>/dev/null")
                    os.system(f"ip route add {subnet} dev {info['ifname']} scope link 2>/dev/null")
                    routing_table[subnet] = {k: v for k, v in info.items() if k != "ifname"}
                    print(f"[LOCAL] reclaimed {subnet} (was incorrectly learned from {old_hop})")
                    # Re-apply ip route replace for every learned route.
                    # Some may have failed silently while the kernel route above was
                    # corrupted (e.g. a route via 10.0.3.20 was processed in the same
                    # packet that also corrupted 10.0.3.0/24). ip route replace is
                    # idempotent so this is safe for routes that already succeeded.
                    for s, sinfo in routing_table.items():
                        if sinfo["learned_from"] is not None:
                            os.system(f"ip route replace {s} via {sinfo['next_hop']} 2>/dev/null")
                    print_table()

        with table_lock:
            snapshot = dict(routing_table)

        for neighbor in NEIGHBORS:
            routes = []
            for subnet, info in snapshot.items():
                if info["learned_from"] == neighbor:
                    # Split Horizon: don't advertise a route back to where we learned it
                    continue
                routes.append({"subnet": subnet, "distance": info["distance"]})

            packet = json.dumps({
                "router_id": MY_IP,
                "version": 1.0,
                "routes": routes
            }).encode()

            try:
                sock.sendto(packet, (neighbor, PORT))
            except OSError as e:
                print(f"[WARN] send to {neighbor} failed: {e}")

        time.sleep(BROADCAST_INTERVAL)


def listen_for_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", PORT))
    print(f"[INFO] {MY_IP} listening on :{PORT}")

    while True:
        try:
            data, addr = sock.recvfrom(65535)
            packet = json.loads(data.decode())
            # Use actual UDP source IP as next_hop — it is guaranteed to be on a
            # directly-connected subnet, unlike router_id which may be the sender's
            # primary IP on a different interface.
            neighbor_ip = addr[0]
            with table_lock:
                neighbor_last_seen[neighbor_ip] = time.time()
            update_logic(neighbor_ip, packet.get("routes", []))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] recv error: {e}")


def update_logic(neighbor_ip, routes_from_neighbor):
    changed = False
    with table_lock:
        for route in routes_from_neighbor:
            subnet = route["subnet"]
            new_dist = route["distance"] + 1

            if new_dist >= INFINITY:
                continue

            # Never accept a DV update for a subnet we are directly connected to.
            # Accepting it would overwrite the kernel's connected route with a via
            # route, breaking all subsequent ip route replace calls through that interface.
            if subnet in local_subnets:
                continue

            current = routing_table.get(subnet)
            if current is None or new_dist < current["distance"]:
                routing_table[subnet] = {
                    "distance": new_dist,
                    "next_hop": neighbor_ip,
                    "learned_from": neighbor_ip,
                }
                os.system(f"ip route replace {subnet} via {neighbor_ip} 2>/dev/null")
                print(f"[UPDATE] {subnet} via {neighbor_ip} cost={new_dist}")
                changed = True

    if changed:
        print_table()


def expire_neighbors():
    while True:
        time.sleep(BROADCAST_INTERVAL)
        now = time.time()
        with table_lock:
            for neighbor, last_seen in list(neighbor_last_seen.items()):
                if now - last_seen <= NEIGHBOR_TIMEOUT:
                    continue
                removed = []
                for subnet, info in list(routing_table.items()):
                    if info["learned_from"] == neighbor:
                        del routing_table[subnet]
                        # Specify via so we only remove the route we added,
                        # not any kernel-added connected route for the same prefix.
                        os.system(f"ip route del {subnet} via {info['next_hop']} 2>/dev/null")
                        removed.append(subnet)
                if removed:
                    print(f"[EXPIRE] neighbor {neighbor} timed out, removed: {removed}")
                    print_table()
                del neighbor_last_seen[neighbor]


def print_table():
    print("\n--- Routing Table ---")
    for subnet, info in sorted(routing_table.items()):
        hop = info["next_hop"]
        dist = info["distance"]
        src = info["learned_from"] or "local"
        print(f"  {subnet:<18} dist={dist}  next_hop={hop:<15} learned_from={src}")
    print("---------------------\n")


if __name__ == "__main__":
    detected = get_local_subnets()
    with table_lock:
        for subnet, info in detected.items():
            local_subnets.add(subnet)
            routing_table[subnet] = {k: v for k, v in info.items() if k != "ifname"}

    print(f"[INFO] Router ID : {MY_IP}")
    print(f"[INFO] Neighbors : {NEIGHBORS}")
    print(f"[INFO] Local nets: {list(routing_table.keys())}")
    print_table()

    threading.Thread(target=broadcast_updates, daemon=True).start()
    threading.Thread(target=expire_neighbors, daemon=True).start()
    listen_for_updates()
