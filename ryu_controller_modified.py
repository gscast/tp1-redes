from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.topology.event import EventSwitchEnter, EventSwitchLeave
from ryu.lib import dpid as dpid_lib
from ryu.lib import stplib
from ryu.lib.mac import haddr_to_bin
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import ether_types

from topology import load_topology
import networkx as nx

# This function takes as input a networkx graph. It then computes
# the minimum Spanning Tree, and returns it, as a networkx graph.
def compute_spanning_tree(G):
    # The Spanning Tree of G
    def build_graph(vertices, edges):
        graph = {}

        for vertex in vertices:

            graph[vertex] = []

            for edge in edges:
                if vertex in edge:
                    graph[vertex].append(edge)
        
        return graph

    edges = G.edges()
    vertices = G.nodes()

    graph = build_graph(vertices, edges)    

    costs = dict()
    edges = dict()
    for node in graph.keys():
        costs[node] = float('inf')
        edges[node] = None
    
    unvisited_vertices = {k: v for k, v in costs.items()}

    root = graph.keys()[0]

    costs[root] = 0
    del edges[root]
    
    while unvisited_vertices:
        current_node = min(unvisited_vertices.keys(),
                           key=(lambda k: unvisited_vertices[k]))
        del unvisited_vertices[current_node]

        cost = costs[current_node]
        for edge in graph[current_node]:

            node = [n for n in edge if n != current_node][0]

            new_cost = cost + 1
            if (node in unvisited_vertices
                    and new_cost < costs[node]):
                costs[node] = new_cost
                unvisited_vertices[node] = new_cost
                edges[node] = (current_node, node)

    ST = nx.from_edgelist(edges.values())

    return ST

class L2Forwarding(app_manager.RyuApp):
    def __init__(self, *args, **kwargs):
        super(L2Forwarding, self).__init__(*args, **kwargs)

        # Load the topology
        topo_file = 'topology.txt'
        self.G = load_topology(topo_file)

        # For each node in the graph, add an attribute mac-to-port
        for n in self.G.nodes():
            self.G.add_node(n, mactoport={})

        # Compute a Spanning Tree for the graph G
        self.ST = compute_spanning_tree(self.G)

        print self.get_str_topo(self.G)
        print self.get_str_topo(self.ST)

        self.mac_to_port = {}

    # This method returns a string that describes a graph (nodes and edges, with
    # their attributes). You do not need to modify this method.
    def get_str_topo(self, graph):
        res = 'Nodes\tneighbors:port_id\n'

        att = nx.get_node_attributes(graph, 'ports')
        for n in graph.nodes_iter():
            res += str(n)+'\t'+str(att[n])+'\n'

        res += 'Edges:\tfrom->to\n'
        for f in graph:
            totmp = []
            for t in graph[f]:
                totmp.append(t)
            res += str(f)+' -> '+str(totmp)+'\n'

        return res

    # This method returns a string that describes the Mac-to-Port table of a
    # switch in the graph. You do not need to modify this method.
    def get_str_mactoport(self, graph, dpid):
        res = 'MAC-To-Port table of the switch '+str(dpid)+'\n'

        for mac_addr, outport in graph.node[dpid]['mactoport'].items():
            res += str(mac_addr)+' -> '+str(outport)+'\n'

        return res.rstrip('\n')

    @set_ev_cls(EventSwitchEnter)
    def _ev_switch_enter_handler(self, ev):
        print('enter: %s' % ev)

    @set_ev_cls(EventSwitchLeave)
    def _ev_switch_leave_handler(self, ev):
        print('leave: %s' % ev)

    def add_flow(self, datapath, in_port, dst, src, actions):
        ofproto = datapath.ofproto

        match = datapath.ofproto_parser.OFPMatch(
            in_port=in_port,
            dl_dst=haddr_to_bin(dst), dl_src=haddr_to_bin(src))

        mod = datapath.ofproto_parser.OFPFlowMod(
            datapath=datapath, match=match, cookie=0,
            command=ofproto.OFPFC_ADD, idle_timeout=0, hard_timeout=0,
            priority=ofproto.OFP_DEFAULT_PRIORITY,
            flags=ofproto.OFPFF_SEND_FLOW_REM, actions=actions)
        datapath.send_msg(mod)
    
    def update_datapath(self, datapath, in_port):
        dpid = datapath.id
        dpid_neighbours = [str(i) for i in self.ST.neighbors(dpid)]

        try:
            mac_to_port_dpid = self.G.node[dpid]["mactoport"]
        except IndexError:
            mac_to_port_dpid = {}

        ports = self.ST.node[dpid]["ports"]

        index = ports.values().index(in_port)
        inport_node = ports.keys()[index]

        if (in_port in mac_to_port_dpid.values()
                and inport_node not in dpid_neighbours):
            index = mac_to_port_dpid.values().index(in_port)
            mac = mac_to_port_dpid.keys()[index]

            index = ports.values().index(in_port)
            node = ports.keys[index]

            del mac_to_port_dpid[mac]
            del ports[node]
        
        self.G.node[dpid]["mactoport"] = mac_to_port_dpid
        self.G.node[dpid]["ports"] = ports

    def delete_flow(self, datapath):
        ofproto = datapath.ofproto

        mac_to_port_dpid = self.G.node[dpid]["mactoport"]

        for dst in mac_to_port_dpid.keys():
            match = parser.OFPMatch(dl_dst=dst)
            mod = datapath.ofproto_parser.OFPFlowMod(
                datapath, command=ofproto.OFPFC_DELETE,
                out_port=ofproto.OFPP_ANY, out_group=ofproto.OFPG_ANY,
                priority=1, match=match)
            datapath.send_msg(mod)

    # This method is called every time an OF_PacketIn message is received by 
    # the switch. Here we must calculate the best action to take and install
    # a new entry on the switch's forwarding table if necessary
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        ofp_parser = dp.ofproto_parser

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            # ignore lldp packet
            return

        dst = eth.dst
        src = eth.src

        dpid = dp.id
        try:
            mac_to_port_dpid = self.G.node[dpid]["mactoport"]
        except IndexError:
            mac_to_port_dpid = {}

        # learn a mac address to avoid FLOOD next time.
        # if port is in spanning tree.
        mac_to_port_dpid[src] = msg.in_port
        self.G.node[dpid]["mactoport"] = mac_to_port_dpid

        #self.update_datapath(dp, msg.in_port)

        #self.remove_flow(dp, msg.in_port)
        try:
            out_port = mac_to_port_dpid[dst]
            actions = [ofp_parser.OFPActionOutput(out_port)]
        except KeyError:
            ports = self.G.node[dpid]["ports"]
            actions = []

            for out_port in ports.values():
                if out_port != msg.in_port:
                    actions.append(ofp_parser.OFPActionOutput(out_port))

        # install a flow to avoid packet_in next time
        if out_port != ofp.OFPP_FLOOD:
            self.add_flow(dp, msg.in_port, dst, src, actions)
	
	# We create an OF_PacketOut message with action of type FLOOD
	# This simple forwarding action works only for loopless topologies
        out = ofp_parser.OFPPacketOut(
            datapath=dp, buffer_id=msg.buffer_id, in_port=msg.in_port,
            actions=actions)
        dp.send_msg(out)

    @set_ev_cls(stplib.EventTopologyChange, MAIN_DISPATCHER)
    def _topology_change_handler(self, ev):
        dp = ev.dp
        dpid_str = dpid_lib.dpid_to_str(dp.id)
        msg = 'Receive topology change event. Flush MAC table.'

        mac_to_port_dpid = self.G.node[dpid]["mactoport"]

        if dp.id in mac_to_port_dpid:
            self.delete_flow(dp)
            del mac_to_port_dpid[dp.id]
            self.G.node[dpid]["mactoport"] = mac_to_port_dpid

    @set_ev_cls(stplib.EventPortStateChange, MAIN_DISPATCHER)
    def _port_state_change_handler(self, ev):
        dpid_str = dpid_lib.dpid_to_str(ev.dp.id)
        of_state = {stplib.PORT_STATE_DISABLE: 'DISABLE',
                    stplib.PORT_STATE_BLOCK: 'BLOCK',
                    stplib.PORT_STATE_LISTEN: 'LISTEN',
                    stplib.PORT_STATE_LEARN: 'LEARN',
                    stplib.PORT_STATE_FORWARD: 'FORWARD'}
