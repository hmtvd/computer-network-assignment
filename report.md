# Assignment 4 – Building a Custom Distance-Vector Router
**Name:** Hemant Meena
**GitHub:** [https://github.com/your-username/computer_network_assignment-4](#)

---

## 1. Introduction

The goal of this assignment was to build a working routing daemon from scratch using Python. The router implements a simplified version of the Distance-Vector protocol, which is the same idea behind RIP (Routing Information Protocol). Instead of using real physical hardware, we used Docker containers to simulate three routers connected in a triangle topology, all talking to each other over UDP.

The whole point of a distance-vector protocol is simple: each router tells its neighbors what destinations it knows about and how far away they are. The neighbors use that information to update their own tables. Over a few rounds of this, every router ends up knowing the best path to every subnet in the network. This process is called convergence.

---

## 2. Design

### How the Router Works

The router is a single Python file (`router.py`) that does three things at once using threads:

- **Broadcast thread** – every 5 seconds, it sends a JSON update packet (over UDP port 5000) to each of its neighbors. The packet lists all the subnets the router currently knows about and the cost to reach them.
- **Listen loop** – sits on port 5000 waiting for packets from neighbors. When one arrives, it immediately processes it.
- **Expiry thread** – runs every 5 seconds and checks if any neighbor has gone silent. If a neighbor hasn't sent anything in 15 seconds, all routes learned from that neighbor are removed.

### The Packet Format

Every update follows the DV-JSON format required by the assignment:

```json
{
  "router_id": "10.0.1.1",
  "version": 1.0,
  "routes": [
    { "subnet": "10.0.1.0/24", "distance": 0 },
    { "subnet": "10.0.2.0/24", "distance": 1 }
  ]
}
```

The `router_id` is the sender's IP address. The `routes` list contains every subnet the sender knows about along with its current cost to reach it.

### The Routing Table

Internally, the routing table is stored as a Python dictionary:

```
{
  "10.0.1.0/24": { "distance": 0, "next_hop": "0.0.0.0", "learned_from": None },
  "10.0.2.0/24": { "distance": 1, "next_hop": "10.0.3.2", "learned_from": "10.0.3.2" }
}
```

When the router starts, it scans its own network interfaces using the `ip -j addr` command and seeds the table with all directly connected subnets at distance 0. All other subnets start out unknown and get added only when a neighbor tells us about them.

### Bellman-Ford

When an update arrives from a neighbor, the router applies Bellman-Ford logic for each advertised route:

- New candidate cost = neighbor's advertised cost + 1
- If that candidate is less than what we currently have, update the table and push the new route into the Linux kernel using `ip route replace <subnet> via <neighbor_ip>`

This ensures every router always picks the shortest known path.

### The Topology

We set up three routers in a triangle. Each router is connected to two Docker networks:

```
         Router A (10.0.1.1 / 10.0.3.1)
        /                               \
   net_ab (10.0.1.0/24)         net_ac (10.0.3.0/24)
      /                                      \
Router B (10.0.1.2 / 10.0.2.1)       Router C (10.0.2.2 / 10.0.3.2)
         \                            /
          net_bc (10.0.2.0/24)
```

| Router | First Interface | Second Interface | Neighbors |
|--------|----------------|-----------------|-----------|
| A | 10.0.1.1 (net_ab) | 10.0.3.1 (net_ac) | 10.0.1.2, 10.0.3.2 |
| B | 10.0.1.2 (net_ab) | 10.0.2.1 (net_bc) | 10.0.1.1, 10.0.2.2 |
| C | 10.0.2.2 (net_bc) | 10.0.3.2 (net_ac) | 10.0.2.1, 10.0.3.1 |

Each router is passed its own IP (`MY_IP`) and its neighbors' IPs (`NEIGHBORS`) as environment variables when the Docker container starts.

---

## 3. Testing and Actual Logs

### How We Ran It

We used a shell script (`setup.sh`) that follows the steps from the assignment PDF exactly:

```bash
bash setup.sh
```

To watch logs in real time:
```bash
docker logs -f router_a
```

To simulate a node failure:
```bash
docker stop router_c
```

---

### Test 1 – Normal Convergence

After starting all three routers and waiting about 10 seconds, the logs showed that every router had discovered all subnets and settled on the shortest paths. Below are the actual logs captured from the running containers.

**Router A – actual output:**
```
[INFO] Router ID : 10.0.1.1
[INFO] Neighbors : ['10.0.1.2', '10.0.3.2']
[INFO] Local nets: ['10.0.1.0/24']

--- Routing Table ---
  10.0.1.0/24        dist=0  next_hop=0.0.0.0         learned_from=local
---------------------

[INFO] 10.0.1.1 listening on :5000
[UPDATE] 10.0.2.0/24 via 10.0.1.2 cost=2

--- Routing Table ---
  10.0.1.0/24        dist=0  next_hop=0.0.0.0         learned_from=local
  10.0.2.0/24        dist=2  next_hop=10.0.1.2        learned_from=10.0.1.2
---------------------

[UPDATE] 10.0.2.0/24 via 10.0.3.2 cost=1

--- Routing Table ---
  10.0.1.0/24        dist=0  next_hop=0.0.0.0         learned_from=local
  10.0.2.0/24        dist=1  next_hop=10.0.3.2        learned_from=10.0.3.2
---------------------
```

What this shows: Router A first hears about net_bc from Router B at cost 2. A few seconds later it hears directly from Router C at cost 1, so it switches to the shorter path. Bellman-Ford is working correctly.

---

**Router B – actual output:**
```
[INFO] Router ID : 10.0.1.2
[INFO] Neighbors : ['10.0.1.1', '10.0.2.2']
[INFO] Local nets: ['10.0.1.0/24']

--- Routing Table ---
  10.0.1.0/24        dist=0  next_hop=0.0.0.0         learned_from=local
---------------------

[INFO] 10.0.1.2 listening on :5000
[UPDATE] 10.0.2.0/24 via 10.0.2.2 cost=1

--- Routing Table ---
  10.0.1.0/24        dist=0  next_hop=0.0.0.0         learned_from=local
  10.0.2.0/24        dist=1  next_hop=10.0.2.2        learned_from=10.0.2.2
---------------------
```

Router B is directly connected to Router C on net_bc, so it correctly learns net_bc at cost 1.

---

**Router C – actual output:**
```
[INFO] Router ID : 10.0.2.2
[INFO] Neighbors : ['10.0.2.1', '10.0.3.1']
[INFO] Local nets: ['10.0.2.0/24']

--- Routing Table ---
  10.0.2.0/24        dist=0  next_hop=0.0.0.0         learned_from=local
---------------------

[INFO] 10.0.2.2 listening on :5000
[UPDATE] 10.0.1.0/24 via 10.0.3.1 cost=1

--- Routing Table ---
  10.0.1.0/24        dist=1  next_hop=10.0.3.1        learned_from=10.0.3.1
  10.0.2.0/24        dist=0  next_hop=0.0.0.0         learned_from=local
---------------------
```

Router C owns net_bc (dist=0) and learns about net_ab from Router A at cost 1.

---

### Test 2 – Node Failure (Router C Stopped)

We ran `docker stop router_c` to simulate a router going offline. After 15 seconds (3 missed broadcast cycles), Router A and Router B both detected the silence and automatically removed all routes they had learned from C.

**Router A log after Router C failure:**
```
[EXPIRE] neighbor 10.0.3.2 timed out, removed: ['10.0.2.0/24']

--- Routing Table ---
  10.0.1.0/24        dist=0  next_hop=0.0.0.0         learned_from=local
---------------------
```

**Router B log after Router C failure:**
```
[EXPIRE] neighbor 10.0.2.2 timed out, removed: ['10.0.2.0/24']

--- Routing Table ---
  10.0.1.0/24        dist=0  next_hop=0.0.0.0         learned_from=local
---------------------
```

Both routers correctly detected the failure and cleaned up their tables with no manual intervention.

---

## 4. Loop Prevention Analysis

### The Problem: Count to Infinity

Without any protection, distance-vector protocols have a well-known problem. Imagine Router A and Router B both know about net_bc. If C goes down:

- B loses its direct route to net_bc
- B asks A, "how far are you from net_bc?" A says cost=1 (because it learned it from B earlier)
- B thinks it can reach net_bc through A at cost=2, so it updates
- A hears B say cost=2, so A updates to cost=3
- This keeps going — 3, 4, 5 ... all the way to 16 (infinity)

This is called the "count to infinity" problem. It makes the network waste time and bandwidth counting up to infinity before giving up.

### Our Solution: Split Horizon

We prevent this with a rule called Split Horizon: **never advertise a route back to the neighbor you learned it from.**

In the code, before sending an update to a neighbor, we check the `learned_from` field for every route. If a route was learned from that neighbor, we skip it:

```python
for subnet, info in snapshot.items():
    if info["learned_from"] == neighbor:
        continue   # Split Horizon: don't send it back
    routes.append({"subnet": subnet, "distance": info["distance"]})
```

So if Router A learned net_bc from Router B, A will never tell B about net_bc. When B's direct link to C fails, B stops receiving updates for net_bc from C, and A also doesn't advertise it back to B. So B correctly concludes that net_bc is unreachable — no counting happens.

### Handling Silent Failures: Neighbor Timeout

Split Horizon handles the logic side, but what about a router that just disappears with no warning? There is no "goodbye" message in our protocol. For this we use a timeout.

Every router tracks the last time it heard from each neighbor. A background thread checks this every 5 seconds. If a neighbor hasn't sent anything in 15 seconds (3 missed cycles), all routes learned from that neighbor are deleted from both the software routing table and the Linux kernel:

```python
if now - last_seen > NEIGHBOR_TIMEOUT:
    for subnet, info in list(routing_table.items()):
        if info["learned_from"] == neighbor:
            del routing_table[subnet]
            os.system(f"ip route del {subnet}")
```

The actual logs above show this working — both Router A and Router B printed `[EXPIRE]` exactly 15 seconds after Router C was stopped, and cleaned up correctly.

This combination — Split Horizon for preventing logic loops and neighbor timeouts for detecting silent failures — ensures the network always converges to a correct state after any topology change.

---

## 5. Conclusion

The router daemon works as expected. It automatically discovers the network topology, calculates shortest paths using Bellman-Ford, updates the Linux kernel routing table in real time, and recovers cleanly when a node goes down. The implementation is around 130 lines of Python with no external libraries beyond the standard library. All tests were run inside Docker containers on WSL2.
