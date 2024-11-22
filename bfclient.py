import sys, socket, json, time 
from select import select
from collections import defaultdict, namedtuple
from threading import Thread, Timer
from datetime import datetime
from copy import deepcopy

SIZE = 4096

# user commands and inter-node protocol update types
# note: set of commands and set of protocol messages intersect! 
#       if you want a list see user_cmds and udpates near main.
LINKDOWN      = "linkdown"
LINKUP        = "linkup"
LINKCHANGE    = "linkchange"
SHOWRT        = "showrt"
CLOSE         = "close"
COSTSUPDATE   = "costsupdate"
SHOWNEIGHBORS = "neighbors"

class RepeatTimer(Thread):
    """ thread that will call a function every interval seconds """
    def __init__(self, interval, target):
        Thread.__init__(self)
        self.target = target
        self.interval = interval
        self.daemon = True
        self.stopped = False
    def run(self):
        while not self.stopped:
            time.sleep(self.interval)
            self.target()

class ResettableTimer():
    def __init__(self, interval, func, args=None):
        if args != None: assert type(args) is list
        self.interval = interval
        self.func = func
        self.args = args
        self.countdown = self.create_timer()
    def start(self):
        self.countdown.start()
    def reset(self):
        self.countdown.cancel()
        self.countdown = self.create_timer()
        self.start()
    def create_timer(self):
        t = Timer(self.interval, self.func, self.args)
        t.daemon = True
        return t
    def cancel(self):
        self.countdown.cancel()

def estimate_costs():
    """ recalculate inter-node path costs using bellman ford algorithm """
    for destination_addr, destination in nodes.items():
        # we don't need to update the distance to ourselves
        if destination_addr != me:
            # iterate through neighbors and find cheapest route
            cost = float("inf")
            nexthop = ''
            for neighbor_addr, neighbor in get_neighbors().items():
                # distance = direct cost to neighbor + cost from neighbor to destination
                if destination_addr in neighbor['costs']:
                    dist = neighbor['direct'] + neighbor['costs'][destination_addr]
                    if dist < cost:
                        cost = dist
                        nexthop = neighbor_addr
            # set new estimated cost to node in the network
            destination['cost'] = cost
            destination['route'] = nexthop

def update_costs(host, port, **kwargs):
    """ update neighbor's costs """
    costs = kwargs['costs']
    addr = addr2key(host, port)
    # if a node listed in costs is not in our list of nodes...
    for node in costs:
        if node not in nodes:
            # ... create a new node
            nodes[node] = default_node()
    # if node not a neighbor ...
    if not nodes[addr]['is_neighbor']: 
        # ... make it your neighbor!
        print('making new neighbor {0}\n'.format(addr))
        del nodes[addr]
        nodes[addr] = create_node(
                cost        = nodes[addr]['cost'], 
                is_neighbor = True,
                direct      = kwargs['neighbor']['direct'],
                costs       = costs,
                addr        = addr)
    else:
        # otherwise just update node costs
        node = nodes[addr]
        node['costs'] = costs
        # restart silence monitor
        node['silence_monitor'].reset()
    # run bellman ford
    estimate_costs()

def broadcast_costs():
    """ send estimated path costs to each neighbor """
    costs = { addr: node['cost'] for addr, node in nodes.items() }
    data = { 'type': COSTSUPDATE }
    for neighbor_addr, neighbor in get_neighbors().items():
        # poison reverse!!! muhuhhahaha
        poisoned_costs = deepcopy(costs)
        for dest_addr, cost in costs.items():
            # only do poisoned reverse if destination not me or neighbor
            if dest_addr not in [me, neighbor_addr]:
                # if we route through neighbor to get to destination ...
                if nodes[dest_addr]['route'] == neighbor_addr:
                    # ... tell neighbor distance to destination is infinty!
                    poisoned_costs[dest_addr] = float("inf")
        data['payload'] = { 'costs': poisoned_costs }
        data['payload']['neighbor'] = { 'direct': neighbor['direct'] }
        # send (potentially 'poisoned') costs to neighbor
        sock.sendto(json.dumps(data), key2addr(neighbor_addr))

def setup_server(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
        print("listening on {0}:{1}\n".format(host, port))
    except socket.error as msg:
        print("an error occured binding the server socket. \
               error code: {0}, msg:{1}\n".format(msg[0], msg[1]))
        sys.exit(1)
    return sock

def default_node():
    return { 'cost': float("inf"), 'is_neighbor': False, 'route': '' }

def create_node(cost, is_neighbor, direct=None, costs=None, addr=None):
    """ centralizes the pattern for creating new nodes """
    node = default_node()
    node['cost'] = cost
    node['is_neighbor'] = is_neighbor
    node['direct'] = direct if direct != None else float("inf")
    node['costs']  = costs  if costs  != None else defaultdict(lambda: float("inf"))
    if is_neighbor:
        node['route'] = addr
        # ensure neighbor is transmitting cost updates using a resettable timer
        monitor = ResettableTimer(
            interval = 3*run_args.timeout, 
            func = linkdown,
            args = list(key2addr(addr)))
        monitor.start()
        node['silence_monitor'] = monitor
    return node

def get_node(host, port):
    """ returns formatted address and node info for that addr """
    error = False
    addr = addr2key(get_host(host), port)
    if not in_network(addr):
        error = 'node not in network'
    node = nodes[addr]
    return node, addr, error

def linkchange(host, port, **kwargs):
    node, addr, err = get_node(host, port)
    if err: return
    if not node['is_neighbor']: 
        print("node {0} is not a neighbor so the link cost can't be changed\n".format(addr))
        return
    direct = kwargs['direct']
    if direct < 1:
        print("the minimum amount a link cost between nodes can be is 1")
        return
    if 'saved' in node:
        print("this link currently down. please first bring link back to life using LINKUP cmd.")
        return
    node['direct'] = direct
    # run bellman-ford
    estimate_costs()

def linkdown(host, port, **kwargs):
    node, addr, err = get_node(host, port)
    if err: return
    if not node['is_neighbor']: 
        print("node {0} is not a neighbor so it can't be taken down\n".format(addr))
        return
    # save direct distance to neighbor, then set to infinity
    node['saved'] = node['direct']
    node['direct'] = float("inf")
    node['is_neighbor'] = False
    node['silence_monitor'].cancel()
    # run bellman-ford
    estimate_costs()

def linkup(host, port, **kwargs):
    node, addr, err = get_node(host, port)
    if err: return
    # make sure node was previously taken down via LINKDOWN cmd
    if 'saved' not in node:
        print("{0} wasn't a previous neighbor\n".format(addr))
        return
    # restore saved direct distance
    node['direct'] = node['saved']
    del node['saved']
    node['is_neighbor'] = True
    # run bellman-ford
    estimate_costs()

def formatted_now():
    return datetime.now().strftime("%b-%d-%Y, %I:%M %p, %S seconds")

def show_neighbors():
    """ show active neighbors """
    print(formatted_now())
    print("Neighbors: ")
    for addr, neighbor in get_neighbors().items():
        print("{addr}, cost:{cost}, direct:{direct}".format(
                addr   = addr, 
                cost   = neighbor['cost'],
                direct = neighbor['direct']))
    print() # extra line

def showrt():
    """ display routing info: cost to destination; route to take """
    print(formatted_now())
    print("Distance vector list is:")
    for addr, node in nodes.items():
        if addr != me:
            print("Destination = {destination}, "
                   "Cost = {cost}, "
                   "Link = ({nexthop})".format(
                        destination = addr,
                        cost        = node['cost'],
                        nexthop     = node['route']))
    print() # extra line

def close():
    """ notify all neighbors that she's a comin daaaahwn! then close process"""
    ''' I am commenting out the code that instantly notifies neighbors that the link
        is being closed because requirements say a 'close' cmd "is like simulating link failure"
    data = {'type': LINKDOWN}
    for neighbor in get_neighbors().values():
        addr = key2addr(neighbor)
        sock.sendto(json.dumps(data), addr)
    print("done notifying neighbors\n") '''
    sys.exit()

def run_server():
    global nodes
    args = ('localhost', 1234)  # Replace with the desired host and port
    sock = setup_server(*args)
    print("waiting for incoming data\n")
    try:
        while True:
            read_sockets, write_sockets, error_sockets = select([sock], [], [], 3)
            # if no packets arrive, keep going without blocking
            if not read_sockets:
                continue
            for s in read_sockets:
                data, address = s.recvfrom(SIZE)
                # handle data
                try:
                    data = json.loads(data)
                except ValueError as err:
                    print("Error decoding json: {0}".format(err))
                    break
                handle_message(data, address)
    except KeyboardInterrupt:
        print("Server shutting down...\n")
    finally:
        sock.close()
        
run_server()