from struct import unpack
import getopt
import sys

from twisted.internet import reactor
import twisted.web.client

from coherence.base import Coherence
from coherence.upnp.devices.control_point import ControlPoint
from xml.sax.saxutils import escape

# Channel list types to use as fallbacks if designated channel is not in
# current channel list type. 0x11..0x15 are favourites 1..5, 0x01 is all,
# 0x03 is TV, 0x04 is Radio, 0x06 is analogue. Dunno for the other numbers.
# And it's literally the string, 0xXY.
CL_TYPE_FALLBACKS = '0x11 0x12 0x13 0x14 0x15 0x03 0x04 0x06 0x01'.split(' ')

opts = {
    'loglevel': 'warning',
    'devtype' : 'urn:samsung.com:device:MainTVServer2:1',
    'channel': 'Das Erste HD',
}

class Channel(object):
    def __init__(self, from_dat=None):
        if from_dat:
            self.parse_dat(from_dat)
        else:
            raise ValueError()

    def __repr__(self):
        return '<Channel %s "%s" ChType=%s MajorCh=%d MinorCh=%d PTC=%d ProgNum=%d>' % \
            (self.dispno, self.title, self.ch_type, self.major_ch, self.minor_ch, self.ptc,
             self.prog_num)

    def as_xml(self):
        return ('<?xml version="1.0" encoding="UTF-8" ?><Channel><ChType>%s</ChType><MajorCh>%d'
                '</MajorCh><MinorCh>%d</MinorCh><PTC>%d</PTC><ProgNum>%d</ProgNum></Channel>') % \
            (escape(self.ch_type), self.major_ch, self.minor_ch, self.ptc, self.prog_num)



    def parse_dat(self, buf):
        def _getint(buf, offset):
            # numbers are 16-bit little-endian unsigned
            x = unpack('<H', buf[offset:offset+2])
            return x[0]

        t = _getint(buf, 0)
        if t == 4:
            self.ch_type = 'CDTV'
        elif t == 3:
            self.ch_type = 'CATV'
        else:
            raise ValueError('Unknown channel type %d' % t)

        self.major_ch = _getint(buf, 2)
        self.minor_ch = _getint(buf, 4)
        self.ptc      = _getint(buf, 6)
        self.prog_num = _getint(buf, 8)

        if _getint(buf, 10) != 0xffff:
            raise ValueError('reserved field mismatch (%04x)' %
                             _getint(buf, 10))

        self.dispno = buf[12:16].rstrip('\x00')

        title_len = _getint(buf, 22)
        self.title = buf[24:24+title_len]

def set_channel_returned(result, set_main_tv_channel, cl_type_fallbacks, channel):
    if result['Result'] == 'NOTOK_InvalidCh':
        try:
            cl_type = cl_type_fallbacks.pop(0)
        except IndexError:
            assert False, "FIXME"

        print "channel %s not in current channel list, trying with %s" % \
            (channel, cl_type)
        set_main_tv_channel.call(ChannelListType=cl_type,
                                 SatelliteID=0,
                                 Channel=channel.as_xml()).\
                        addCallback(set_channel_returned, set_main_tv_channel,
                                    cl_type_fallbacks, channel)
    elif result['Result'] == 'OK':
        print "Channel switched."
        reactor.stop()
    else:
        print "result: %s" % result



def _parse_channel_list(channel_list):
    # The channel list is binary file with a 128-byte header (ignored)
    # and 124-byte chunks.
    assert len(channel_list) > (128+124), 'At least one channel needed'
    assert (len(channel_list)-128) % 124 == 0, 'Incomplete chunk'

    channels = []
    pos = 128
    while pos < len(channel_list):
        channels.append(Channel(channel_list[pos:pos+124]))
        pos += 124
    return channels

def got_channel_list(channel_list, cl_type, service):
    channels = _parse_channel_list(channel_list)
    channel_ = [c for c in channels if c.title == opts['channel']]
    assert len(channel_) > 0, 'No channel found'
    channel = channel_[0]

    print "Found channel: %s" % channel

    set_main_tv_channel = service.get_action('SetMainTVChannel')
    print "cl_type %s, channel as_xml %s" % (cl_type, channel.as_xml())
    set_main_tv_channel.call(ChannelListType=cl_type,
                             SatelliteID=0,
                             Channel=channel.as_xml()).\
                    addCallback(set_channel_returned,
                                set_main_tv_channel,
                                CL_TYPE_FALLBACKS[:],
                                channel)

def got_channel_list_url(results, service):
    print "got_channel_list_url: %s" % results
    # A string, like 0x12
    cl_type = results['ChannelListType']

    twisted.web.client.getPage(results['ChannelListURL']).\
        addCallback(got_channel_list, cl_type, service)

def dev_found(device):
    if opts['devtype']:
        if device.get_device_type() != opts['devtype']:
            return

    services = [s for s in device.services if s.get_id() ==
                'urn:samsung.com:serviceId:MainTVAgent2']

    if not services:
        return
    assert len(services) == 1, "Can't handle more than one service"
    svc = services[0]

    get_channel_list_url = svc.get_action('GetChannelListURL')

    # UPnP somehow maps action's return values to state variables. If Coherence knows
    # of such a mapping, each value returned has to exist in that mapping. If not,
    # Action.got_results will crash with an IndexError because of the faied lookup.
    # As a workaround, pretend that there are no "out" arguments (with no related
    # state variables to updates).
    get_channel_list_url.arguments_list = get_channel_list_url.get_in_arguments()

    get_channel_list_url.call().addCallback(got_channel_list_url, svc)


def start():
    """Starts up Coherence and sets everything up. Next: dev_found()"""
    config = {}
    config['logmode'] = opts['loglevel']

    c = Coherence(config)
    cp = ControlPoint(c, auto_client=[])
    cp.connect(dev_found, 'Coherence.UPnP.RootDevice.detection_completed')

def main():
    """Parse options and start twisted.reactor. Next: start()"""
    try:
        gopts, rest_ = getopt.getopt(sys.argv[1:], "l:t:c:", ["loglevel", "devtype", "channel"])
    except getopt.GetoptError as err:
        print str(err)
        sys.exit(1)

    for o, a in gopts:
        if o == '-l':
            opts['loglevel'] = a
        elif o == '-t':
            opts['devtype'] = a
        elif o == '-c':
            opts['channel'] = a
        else:
            sys.stderr.write('Unknown option: %s\n', o)
            sys.exit(1)

    reactor.callWhenRunning(start)
    reactor.run()

if __name__ == '__main__':
    main()
