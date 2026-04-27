import socket
import json
import threading
import time
import os
import subprocess
import ipaddress

MY_IP = os.getenv("MY_IP", "0.0.0.0")
NEIGHBORS = [n.strip() for n in os.getenv("NEIGHBORS", "").split(",") if n.strip()]
PORT = 5000
INFINITY = 16
UPDATE_INTERVAL = 1   # seconds between periodic broadcasts
TIMEOUT = 4           # seconds before a learned route is considered stale

# {subnet_cidr: {"distance": int, "next_hop": str}}
routing_table = {}
# {subnet_cidr: last_seen_timestamp}
last_updated = {}
table_lock = threading.Lock()

# List of ipaddress.IPv4Network objects for our directly-connected interfaces.
# Populated after the startup delay so Docker has time to attach all networks.
local_subnets = []


def get_local_subnets():
    out = subprocess.check_output(["ip", "-o", "-4", "addr", "show"]).decode()
    nets = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "inet" and not parts[3].startswith("127."):
            nets.append(ipaddress.ip_network(parts[3], strict=False))
    return nets


def refresh_local_subnets():
    """Re-scan interfaces and seed/reclaim directly-connected subnets."""
    global local_subnets
    detected = get_local_subnets()
    with table_lock:
        for net in detected:
            net_str = str(net)
            curr = routing_table.get(net_str)
            if curr is None or curr["distance"] != 0:
                if curr is not None and curr["distance"] != 0:
                    # Was incorrectly learned via DV — fix kernel route
                    os.system(f"ip route del {net_str} via {curr['next_hop']} 2>/dev/null")
                routing_table[net_str] = {"distance": 0, "next_hop": "0.0.0.0"}
                last_updated[net_str] = time.time()
                print(f"[LOCAL] {net_str} seeded/reclaimed as directly connected")
        local_subnets = detected


def is_local_subnet(subnet_str):
    for net in local_subnets:
        if subnet_str == str(net):
            return True
    return False


def is_directly_connected(sender_ip):
    """Return True only if sender is on one of our directly-connected networks."""
    try:
        ip = ipaddress.ip_address(sender_ip)
        return any(ip in net for net in local_subnets)
    except ValueError:
        return False


def sync_kernel(subnet, distance, next_hop):
    """Mirror a routing table entry into the Linux kernel."""
    if distance >= INFINITY:
        os.system(f"ip route del {subnet} 2>/dev/null")
    elif next_hop != "0.0.0.0":
        os.system(f"ip route replace {subnet} via {next_hop} 2>/dev/null")


def broadcast_updates():
    """Send the current routing table to every neighbor (with Poison Reverse)."""
    with table_lock:
        snapshot = {k: dict(v) for k, v in routing_table.items()}

    for neighbor in NEIGHBORS:
        routes = {}
        for subnet, info in snapshot.items():
            dist = info["distance"]
            # Poison Reverse: advertise INFINITY back to the neighbor we learned from
            if info["next_hop"] == neighbor:
                dist = INFINITY
            routes[subnet] = dist

        packet = json.dumps({
            "router_id": MY_IP,
            "version": 1.0,
            "routes": [{"subnet": s, "distance": d} for s, d in routes.items()]
        }).encode()

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto(packet, (neighbor, PORT))
        except OSError:
            pass


def periodic_broadcast():
    while True:
        broadcast_updates()
        time.sleep(UPDATE_INTERVAL)


def listen_for_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", PORT))
    print(f"[INFO] {MY_IP} listening on :{PORT}")

    while True:
        try:
            data, addr = sock.recvfrom(65535)
            sender_ip = addr[0]

            # Only accept updates from directly-connected neighbors
            if not is_directly_connected(sender_ip):
                continue

            packet = json.loads(data.decode())
            routes = packet.get("routes", [])
        except (OSError, json.JSONDecodeError):
            continue

        triggered = False
        with table_lock:
            for route in routes:
                subnet = route["subnet"]
                new_dist = min(route["distance"] + 1, INFINITY)

                # Never overwrite our own directly-connected subnets
                if is_local_subnet(subnet):
                    continue

                curr = routing_table.get(subnet)

                if curr is None:
                    if new_dist < INFINITY:
                        routing_table[subnet] = {"distance": new_dist, "next_hop": sender_ip}
                        last_updated[subnet] = time.time()
                        sync_kernel(subnet, new_dist, sender_ip)
                        triggered = True

                else:
                    cur_dist = curr["distance"]
                    cur_hop = curr["next_hop"]

                    if sender_ip == cur_hop:
                        # Always trust the current next-hop's update (catches increases too)
                        last_updated[subnet] = time.time()
                        if cur_dist != new_dist:
                            routing_table[subnet] = {"distance": new_dist, "next_hop": sender_ip}
                            sync_kernel(subnet, new_dist, sender_ip)
                            triggered = True

                    elif new_dist < cur_dist:
                        routing_table[subnet] = {"distance": new_dist, "next_hop": sender_ip}
                        last_updated[subnet] = time.time()
                        sync_kernel(subnet, new_dist, sender_ip)
                        triggered = True

        if triggered:
            broadcast_updates()


def monitor_timeouts():
    """Expire learned routes that haven't been refreshed within TIMEOUT seconds."""
    while True:
        time.sleep(0.5)
        now = time.time()
        triggered = False

        with table_lock:
            for subnet in list(routing_table.keys()):
                if is_local_subnet(subnet):
                    continue
                info = routing_table[subnet]
                if info["distance"] < INFINITY and now - last_updated.get(subnet, 0) > TIMEOUT:
                    print(f"[EXPIRE] {subnet} timed out")
                    routing_table[subnet]["distance"] = INFINITY
                    sync_kernel(subnet, INFINITY, info["next_hop"])
                    triggered = True

        if triggered:
            broadcast_updates()


def print_table():
    print("\n--- Routing Table ---")
    for subnet, info in sorted(routing_table.items()):
        print(f"  {subnet:<18} dist={info['distance']}  next_hop={info['next_hop']}")
    print("---------------------\n")


if __name__ == "__main__":
    # Wait for Docker to finish attaching all networks before scanning.
    # Without this delay the second/third interfaces may not be up yet,
    # causing us to accept DV updates for our own subnets and corrupting
    # the kernel's connected routes.
    time.sleep(2)
    refresh_local_subnets()

    print(f"[INFO] Router ID : {MY_IP}")
    print(f"[INFO] Neighbors : {NEIGHBORS}")
    print(f"[INFO] Local nets: {[str(s) for s in local_subnets]}")
    print_table()

    threading.Thread(target=periodic_broadcast, daemon=True).start()
    threading.Thread(target=monitor_timeouts, daemon=True).start()

    # Periodically re-scan interfaces (handles late network attachments)
    def refresh_loop():
        while True:
            time.sleep(5)
            refresh_local_subnets()
    threading.Thread(target=refresh_loop, daemon=True).start()

    listen_for_updates()
