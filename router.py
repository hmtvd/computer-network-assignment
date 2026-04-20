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
BROADCAST_INTERVAL = 5
NEIGHBOR_TIMEOUT = 15

# {subnet_cidr: {"distance": int, "next_hop": str, "learned_from": str|None}}
routing_table = {}
table_lock = threading.Lock()

# {neighbor_ip: last_seen_timestamp}
neighbor_last_seen = {}


def get_local_subnets():
    result = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True)
    subnets = {}
    for iface in json.loads(result.stdout):
        if iface.get("ifname") == "lo":
            continue
        for addr in iface.get("addr_info", []):
            if addr.get("family") == "inet":
                net = ipaddress.IPv4Network(
                    f"{addr['local']}/{addr['prefixlen']}", strict=False
                )
                subnets[str(net)] = {"distance": 0, "next_hop": "0.0.0.0", "learned_from": None}
    return subnets


def broadcast_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    while True:
        # Re-scan interfaces each cycle to pick up interfaces attached after startup
        for subnet, info in get_local_subnets().items():
            with table_lock:
                if subnet not in routing_table:
                    routing_table[subnet] = info
                    print(f"[LOCAL] new interface subnet detected: {subnet}")
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
            # Use the actual source IP (addr[0]) as next_hop so ip route replace works.
            # router_id may be an IP on a different subnet and would not be directly reachable.
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

            current = routing_table.get(subnet)
            if current is None or new_dist < current["distance"]:
                routing_table[subnet] = {
                    "distance": new_dist,
                    "next_hop": neighbor_ip,
                    "learned_from": neighbor_ip,
                }
                if new_dist > 0:
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
                        os.system(f"ip route del {subnet} 2>/dev/null")
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
    local = get_local_subnets()
    with table_lock:
        routing_table.update(local)

    print(f"[INFO] Router ID : {MY_IP}")
    print(f"[INFO] Neighbors : {NEIGHBORS}")
    print(f"[INFO] Local nets: {list(local.keys())}")
    print_table()

    threading.Thread(target=broadcast_updates, daemon=True).start()
    threading.Thread(target=expire_neighbors, daemon=True).start()
    listen_for_updates()
