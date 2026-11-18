import ipaddress

from . import settings


class Pool:
    def __init__(self, prefix):
        net = ipaddress.ip_network(prefix, strict=False)

        if net.version != 6:
            raise ValueError("need ipv6")
        if net.prefixlen > settings.LANE_BITS:
            raise ValueError("prefix too small")

        count = net.num_addresses // (1 << settings.LANE_BITS)
        if count > settings.MAX_LANES:
            raise ValueError("prefix too large")

        self.lanes = []
        for subnet in net.subnets(new_prefix=settings.LANE_BITS):
            self.lanes.append(str(subnet.network_address + settings.HOST_OFFSET))
