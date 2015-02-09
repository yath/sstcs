from struct import unpack
import getopt
import sys

from twisted.internet import reactor
from twisted.python.failure import Failure
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

# failure is just any "context", but name it 'failure' so we can use it as twisted errback
def fatal(msg, failure=None):
    if isinstance(failure, Failure):
        failure = failure.value

    sys.stderr.write("FATAL ERROR: %s\n" % msg)
    if failure:
        sys.stderr.write("Additional information:\n%s\n" % str(failure))

    # that's wrong, FIXME
    if reactor.running:
        reactor.stop()
        reactor.crash()

    sys.exit(2)


class ContextException(Exception):
    def __init__(self, msg, context=[]):
        self.msg     = msg
        self.context = list(context)

    def __str__(self):
        if self.context:
            return '%s [context: %s]' % (self.msg, '; '.join(self.context))
        else:
            return self.msg

    def add_context(self, context):
        self.context.append(context)


class ParseException(ContextException):
    pass


class Channel(object):
    def __init__(self, from_dat):
        self._parse_dat(from_dat)

    def _parse_dat(self, buf):
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
            raise ParseException('Unknown channel type %d' % t)

        self.major_ch = _getint(buf, 2)
        self.minor_ch = _getint(buf, 4)
        self.ptc      = _getint(buf, 6)
        self.prog_num = _getint(buf, 8)

        if _getint(buf, 10) != 0xffff:
            raise ParseException('reserved field mismatch (%04x)' % _getint(buf, 10))

        self.dispno = buf[12:16].rstrip('\x00')

        title_len = _getint(buf, 22)
        self.title = buf[24:24+title_len]

    def __repr__(self):
        return '<Channel %s "%s" ChType=%s MajorCh=%d MinorCh=%d PTC=%d ProgNum=%d>' % \
            (self.dispno, self.title, self.ch_type, self.major_ch, self.minor_ch, self.ptc,
             self.prog_num)

    def as_xml(self):
        return ('<?xml version="1.0" encoding="UTF-8" ?><Channel><ChType>%s</ChType><MajorCh>%d'
                '</MajorCh><MinorCh>%d</MinorCh><PTC>%d</PTC><ProgNum>%d</ProgNum></Channel>') % \
            (escape(self.ch_type), self.major_ch, self.minor_ch, self.ptc, self.prog_num)


def set_channel_returned(result, set_main_tv_channel, cl_type_fallbacks, channel):
    if result['Result'] == 'NOTOK_InvalidCh':
        try:
            cl_type = cl_type_fallbacks.pop(0)
        except IndexError:
            fatal('TV doesn\'t know how to switch to %s' % channel)

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
        fatal('TV reported back result %s, no idea what that is.', result)


def _parse_channel_list(channel_list):
    # The channel list is binary file with a 128-byte header (ignored)
    # and 124-byte chunks.
    if len(channel_list) < 252:
        raise ParseException(('channel list is smaller than it has to be for at least'\
                              'one channel (%d bytes (actual) vs. 252 bytes' % len(channel_list)),
                             ('Channel list: %s' % repr(channel_list)))

    if (len(channel_list)-128) % 124 != 0:
        raise ParseException(('channel list\'s size (%d) minus 128 (header) is not a multiple of'\
                              '124 bytes' % len(channel_list)),
                             ('Channel list: %s' % repr(channel_list)))

    channels = []
    pos = 128
    while pos < len(channel_list):
        chunk = channel_list[pos:pos+124]
        try:
            channels.append(Channel(chunk))
        except ParseException as pe:
            pe.add_context('chunk starting at %d: %s' % (pos, repr(chunk)))
            raise pe

        pos += 124
    return channels


def got_channel_list(channel_list, cl_type, service):
    try:
        all_channels = _parse_channel_list(channel_list)
    except Exception, e:
        fatal('Unable to parse channel list', e)

    matching_channels = [c for c in all_channels if c.title == opts['channel']]

    if len(matching_channels) == 0:
        fatal('No channel found')

    if len(matching_channels) > 1:
        print "More than one matching channel found (%s), picking first" % matching_channels
    channel = matching_channels[0]

    print "Found channel: %s" % channel

    set_main_tv_channel = service.get_action('SetMainTVChannel')
    if not set_main_tv_channel:
        # FIXME retry, uh?
        fatal('Can\'t resolve SetMainTVChannel on TV, that\'s usually intermittent.')

    print "cl_type %s, channel as_xml %s" % (cl_type, channel.as_xml())

    set_main_tv_channel.call(ChannelListType=cl_type, SatelliteID=0, Channel=channel.as_xml()).\
        addCallback(set_channel_returned, set_main_tv_channel, CL_TYPE_FALLBACKS[:], channel).\
        addErrback(fatal)


def got_channel_list_url(results, service):
    print "got_channel_list_url: %s" % results
    # A string, like 0x12
    cl_type = results['ChannelListType']

    twisted.web.client.getPage(results['ChannelListURL']).\
        addCallback(got_channel_list, cl_type, service).\
        addErrback(fatal)


def dev_found(device):
    if opts['devtype']:
        if device.get_device_type() != opts['devtype']:
            return

    services = [s for s in device.services if s.get_id() ==
                'urn:samsung.com:serviceId:MainTVAgent2']

    if not services:
        return
    if len(services) > 1:
        fatal('Your TV reports back more than one service, can\'t handle that',
              [device, services])

    svc = services[0]

    get_channel_list_url = svc.get_action('GetChannelListURL')
    if not get_channel_list_url:
        # FIXME retry
        fatal('Can\'t resolve GetChannelListURL on TV, that\'s usually intermittent.')

    # UPnP somehow maps action's return values to state variables. If Coherence knows
    # of such a mapping, each value returned has to exist in that mapping. If not,
    # Action.got_results will crash with an IndexError because of the faied lookup.
    # As a workaround, pretend that there are no "out" arguments (with no related
    # state variables to updates).
    get_channel_list_url.arguments_list = get_channel_list_url.get_in_arguments()

    get_channel_list_url.call().addCallback(got_channel_list_url, svc).\
        addErrback(fatal)


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
            sys.stderr.write("Unknown option: %s\n", o)
            sys.exit(1)

    reactor.callWhenRunning(start)
    reactor.run()

if __name__ == '__main__':
    main()
