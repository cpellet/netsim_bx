# Partially based off https://www.grotto-networking.com/DiscreteEventPython.html

import simpy
import random
from typing import Callable, Optional
from collections import defaultdict as dd

class Packet(object):
    def __init__(self,
                 time: float,
                 size: float,
                 packet_id: int,
                 realtime = 0,
                 src="source",
                 dst="destination",
                 flow_id = 0,
                 payload = None):
        self.time = time
        self.size = size
        self.packet_id = packet_id
        self.realtime = realtime
        self.src = src
        self.dst = dst
        self.flow_id = flow_id
        self.payload = payload

        self.color = None  # Used by the two-rate tri-color token bucket shaper
        self.prio = {}  # used by the Static Priority scheduler
        self.ack = 0  # used by TCPPacketGenerator and TCPSink
        self.current_time = 0  # used by the Wire element
        self.perhop_time = {}  # used by Port to record per-hop arrival times

    def __repr__(self):
        return f"id: {self.packet_id}, src: {self.src}, time: {self.time}, size: {self.size}"

class PacketSource(object):
    def __init__(self,
                 env: simpy.Environment,
                 element_id: str,
                 arrival_dist: Callable[[], float],
                 size_dist: Callable[[], float],
                 initial_delay=0,
                 finish=float("inf"),
                 flow_id=0,
                 rec_flow=False,
                 debug=False):
        self.element_id = element_id
        self.env = env
        self.arrival_dist = arrival_dist
        self.size_dist = size_dist
        self.initial_delay = initial_delay
        self.finish = finish
        self.out = None
        self.packets_sent = 0
        self.action = env.process(self.run())
        self.flow_id = flow_id

        self.rec_flow = rec_flow
        self.time_rec = []
        self.size_rec = []
        self.debug = debug

    def run(self):
        """The generator function used in simulations."""
        yield self.env.timeout(self.initial_delay)
        while self.env.now < self.finish:
            # wait for next transmission
            yield self.env.timeout(self.arrival_dist())

            self.packets_sent += 1
            packet = Packet(self.env.now,
                            self.size_dist(),
                            self.packets_sent,
                            src=self.element_id,
                            flow_id=self.flow_id)
            if self.rec_flow:
                self.time_rec.append(packet.time)
                self.size_rec.append(packet.size)

            if self.debug:
                print(
                    f"Sent packet {packet.packet_id} with flow_id {packet.flow_id} at "
                    f"time {self.env.now}.")

            self.out.put(packet)

class PacketSink(object):
    def __init__(self,
                 env: simpy.Environment,
                 rec_arrivals: bool = True,
                 absolute_arrivals: bool = True,
                 rec_waits: bool = True,
                 rec_flow_ids: bool = True,
                 debug: bool = False):
        self.store = simpy.Store(env)
        self.env = env
        self.rec_waits = rec_waits
        self.rec_flow_ids = rec_flow_ids
        self.rec_arrivals = rec_arrivals
        self.absolute_arrivals = absolute_arrivals
        self.waits = dd(list)
        self.arrivals = dd(list)
        self.packets_received = dd(lambda: 0)
        self.bytes_received = dd(lambda: 0)
        self.packet_sizes = dd(list)
        self.packet_times = dd(list)
        self.perhop_times = dd(list)

        self.first_arrival = dd(lambda: 0)
        self.last_arrival = dd(lambda: 0)

        self.debug = debug

    def put(self, packet):
        now = self.env.now

        if self.rec_flow_ids:
            rec_index = packet.flow_id
        else:
            rec_index = packet.src

        if self.rec_waits:
            self.waits[rec_index].append(self.env.now - packet.time)
            self.packet_sizes[rec_index].append(packet.size)
            self.packet_times[rec_index].append(packet.time)
            self.perhop_times[rec_index].append(packet.perhop_time)

        if self.rec_arrivals:
            self.arrivals[rec_index].append(now)
            if len(self.arrivals[rec_index]) == 1:
                self.first_arrival[rec_index] = now

            if not self.absolute_arrivals:
                self.arrivals[rec_index][
                    -1] = now - self.last_arrival[rec_index]

            self.last_arrival[rec_index] = now

        if self.debug:
            print("At time {:.2f}, packet {:d} arrived.".format(
                now, packet.packet_id))
            if self.rec_waits and len(self.packet_sizes[rec_index]) >= 10:
                bytes_received = sum(self.packet_sizes[rec_index][-9:])
                time_elapsed = self.env.now - (
                    self.packet_times[rec_index][-10] +
                    self.waits[rec_index][-10])
                print(
                    "Average throughput (last 10 packets): {:.2f} bytes/second."
                    .format(float(bytes_received) / time_elapsed))

        self.packets_received[rec_index] += 1
        self.bytes_received[rec_index] += packet.size

class SwitchPort(object):
    def __init__(self, env: simpy.Environment, rate: float, qlimit: Optional[float] = None, limit_bytes: bool = True, debug: bool = False):
        self.store = simpy.Store(env)
        self.rate = rate
        self.env = env
        self.out = None
        self.packets_rec = 0
        self.packets_drop = 0
        self.qlimit = qlimit
        self.limit_bytes = limit_bytes
        self.byte_size = 0
        self.debug = debug
        self.busy = 0
        self.action = env.process(self.run())

    def run(self):
        while True:
            msg = (yield self.store.get())
            self.busy = 1
            self.byte_size -= msg.size
            yield self.env.timeout(msg.size*8.0/self.rate)
            self.out.put(msg)
            self.busy = 0
            if self.debug:
                print(msg)

    def put(self, pkt: Packet):
        self.packets_rec += 1
        tmp_byte_count = self.byte_size + pkt.size

        if self.qlimit is None:
            self.byte_size = tmp_byte_count
            return self.store.put(pkt)
        if self.limit_bytes and tmp_byte_count >= self.qlimit:
            self.packets_drop += 1
            return
        elif not self.limit_bytes and len(self.store.items) >= self.qlimit-1:
            self.packets_drop += 1
        else:
            self.byte_size = tmp_byte_count
            return self.store.put(pkt)

class PortMonitor(object):
    def __init__(self, env: simpy.Environment, port: SwitchPort, dist: Callable, count_bytes: bool = False):
        self.port = port
        self.env = env
        self.dist = dist
        self.count_bytes = count_bytes
        self.sizes = []
        self.action = env.process(self.run())

    def run(self):
        while True:
            yield self.env.timeout(self.dist())
            if self.count_bytes:
                total = self.port.byte_size
            else:
                total = len(self.port.store.items) + self.port.busy
            self.sizes.append(total)

class Channel(object):
    def __init__(self, env: simpy.Environment, delay_dist: Callable, loss_dist: Callable = None, channel_id: int = 0, debug: bool = False):
        self.store = simpy.Store(env)
        self.delay_dist = delay_dist
        self.loss_dist = loss_dist
        self.env = env
        self.channel_id = channel_id
        self.out = None
        self.packets_rec = 0
        self.debug = debug
        self.action = env.process(self.run())

    def run(self):
        while True:
            packet = yield self.store.get()
            if self.loss_dist is None or random.uniform(
                    0, 1) >= self.loss_dist(packet_id=packet.packet_id):
                # The amount of time for this packet to stay in store
                queued_time = self.env.now - packet.current_time
                delay = self.delay_dist()
                if queued_time < delay:
                    yield self.env.timeout(delay - queued_time)
                self.out.put(packet)
                if self.debug:
                    print("Left channel #{} at {:.3f}: {}".format(
                        self.channel_id, self.env.now, packet))
            else:
                if self.debug:
                    print("Dropped on channel #{} at {:.3f}: {}".format(
                        self.channel_id, self.env.now, packet))

    def put(self, packet):
        self.packets_rec += 1
        if self.debug:
            print(f"Entered channel #{self.channel_id} at {self.env.now}: {packet}")

        packet.current_time = self.env.now
        return self.store.put(packet)