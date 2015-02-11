from struct import unpack
import getopt
import sys
import codecs

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
    'do_list': False,
}

def fatal(msg, failure=None):
    """This writes an error message to stderr and stops reactor, if it's running.
    Note that you must still return yourself from a Twisted callback or call
    sys.exit() outside Twisted.

    Args:
        msg: The error message
        failure: Any kind of additional information. It's just called 'failure' so we
                 can use it as errback for Twisted.
    """

    if isinstance(failure, Failure):
        failure = failure.value

    sys.stderr.write("FATAL ERROR: %s\n" % msg)
    if failure:
        sys.stderr.write("Additional information:\n%s\n" % str(failure))

    if reactor.running:
        reactor.stop()


class ContextException(Exception):
    """An Exception class with context attached to it, so a caller can catch a
    (subclass of) ContextException, add some context with the exception's
    add_context method, and rethrow it to another callee who might again add
    information."""

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
    """An Exception for when something went wrong parsing the channel list."""
    pass


class Channel(object):
    """Class representing a Channel from the TV's channel list."""

    def __init__(self, from_dat):
        """Constructs the Channel object from a binary channel list chunk."""

        self._parse_dat(from_dat)

    def _parse_dat(self, buf):
        """Parses the binary data from a channel list chunk and initilizes the
        member variables."""

        # Each entry consists of (all integers are 16-bit little-endian unsigned):
        #   [2 bytes int] Type of the channel. I've only seen 3 and 4, meaning
        #                 CDTV (Cable Digital TV, I guess) or CATV (Cable Analog
        #                 TV) respectively as argument for <ChType>
        #   [2 bytes int] Major channel (<MajorCh>)
        #   [2 bytes int] Minor channel (<MinorCh>)
        #   [2 bytes int] PTC (Physical Transmission Channel?), <PTC>
        #   [2 bytes int] Program Number (in the mux'ed MPEG or so?), <ProgNum>
        #   [2 bytes int] They've always been 0xffff for me, so I'm just assuming
        #                 they have to be :)
        #   [4 bytes string, \0-padded] The (usually 3-digit, for me) channel number
        #                               that's displayed (and which you can enter), in ASCII
        #   [2 bytes int] Length of the channel title
        #   [106 bytes string, \0-padded] The channel title, in UTF-8 (wow)

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
        self.title = buf[24:24+title_len].decode('utf-8')

    def display_string(self):
        """Returns a unicode display string, since both __repr__ and __str__ convert it
        to ascii."""

        return u'[%s] % 4s %s' % (self.ch_type, self.dispno, self.title)

    def __repr__(self):
        return '<Channel %s %s ChType=%s MajorCh=%d MinorCh=%d PTC=%d ProgNum=%d>' % \
            (self.dispno, repr(self.title), self.ch_type, self.major_ch, self.minor_ch, self.ptc,
             self.prog_num)

    def as_xml(self):
        """The channel list as XML representation for SetMainTVChannel."""

        return ('<?xml version="1.0" encoding="UTF-8" ?><Channel><ChType>%s</ChType><MajorCh>%d'
                '</MajorCh><MinorCh>%d</MinorCh><PTC>%d</PTC><ProgNum>%d</ProgNum></Channel>') % \
            (escape(self.ch_type), self.major_ch, self.minor_ch, self.ptc, self.prog_num)


def set_channel_returned(result, set_main_tv_channel, cl_type_fallbacks, channel):
    """Called when SetMainTVChannel returns. Extracts the 'Result' field from the result
    and in case the result is 'NOTOK_InvalidCh' calls SetMainTVChannel again, with itself
    as callback the next channel type list from cl_type_fallbacks. Or, just tells the
    user whether the TV succeeded in switching the channel or returned an error."""

    if result['Result'] == 'NOTOK_InvalidCh':
        try:
            cl_type = cl_type_fallbacks.pop(0)
        except IndexError:
            fatal('TV doesn\'t know how to switch to %s' % channel)
            return

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
        return


def _parse_channel_list(channel_list):
    """Splits the binary channel list into channel entry fields and returns a list of Channels."""

    # The channel list is binary file with a 128-byte header (ignored)
    # and 124-byte chunks for each channel. See Channel._parse_dat for
    # how each entry is constructed.

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
    """Called when the channel list URL has been retrieved. Parses the channel list, looks
    for a matching channel and calls SetMainTVChannel with the passed cl_type (channel list
    type), unless opts['do_list'] is true, in which case it just prints the channels and
    terminates Twisted.

    Next: set_channel_returned, passing a list of fallback channel types and everything
    needed to reproduce the call to SetMainTVChannel for the fallback channel lists."""

    try:
        all_channels = _parse_channel_list(channel_list)
    except Exception, e:
        fatal('Unable to parse channel list', e)
        return

    if opts['do_list']:
        for channel in all_channels:
            print channel.display_string()
        reactor.stop()
        return

    matching_channels = [c for c in all_channels if c.title == opts['channel']]

    if len(matching_channels) == 0:
        fatal('No channel found')
        return

    if len(matching_channels) > 1:
        print "More than one matching channel found (%s), picking first" % matching_channels
    channel = matching_channels[0]

    print "Found channel: %s" % channel

    set_main_tv_channel = service.get_action('SetMainTVChannel')
    if not set_main_tv_channel:
        # FIXME retry, uh?
        fatal('Can\'t resolve SetMainTVChannel on TV, that\'s usually intermittent.')
        return

    print "cl_type %s, channel as_xml %s" % (cl_type, channel.as_xml())

    set_main_tv_channel.call(ChannelListType=cl_type, SatelliteID=0, Channel=channel.as_xml()).\
        addCallback(set_channel_returned, set_main_tv_channel, CL_TYPE_FALLBACKS[:], channel).\
        addErrback(fatal)


def got_channel_list_url(results, service):
    """Called when GetChannelListURL returns. Gets the URL referenced from the channel list and
    the channel list type. Next: got_channel_list(channel_list_type)."""

    print "got_channel_list_url: %s" % results
    # A string, like 0x12
    cl_type = results['ChannelListType']

    twisted.web.client.getPage(results['ChannelListURL']).\
        addCallback(got_channel_list, cl_type, service).\
        addErrback(fatal)


def dev_found(device):
    """Called when a device was found and calls GetChannelListURL if the device matches and has
    the appropriate service. Next: got_channel_list_url(service)."""

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
        return

    svc = services[0]

    get_channel_list_url = svc.get_action('GetChannelListURL')
    if not get_channel_list_url:
        # FIXME retry
        fatal('Can\'t resolve GetChannelListURL on TV, that\'s usually intermittent.')
        return

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
    """Parse options, set everything up and start twisted.reactor. Next: start()"""

    # Force stdout to be utf-8 so we can actually pipe our output to grep. Argh.
    sys.stdout = codecs.getwriter('utf8')(sys.stdout)

    try:
        gopts, rest_ = getopt.getopt(sys.argv[1:], "L:t:c:l",
                                     ["loglevel", "devtype", "channel", "list"])
    except getopt.GetoptError as err:
        print str(err)
        sys.exit(1)

    for o, a in gopts:
        if o == '-L':
            opts['loglevel'] = a
        elif o == '-t':
            opts['devtype'] = a
        elif o == '-c':
            try:
                opts['channel'] = a.decode('utf-8')
            except UnicodeDecodeError:
                # any binary string is valid iso-8859-1, so it *should* never
                # raise an error
                opts['channel'] = a.decode('iso-8859-1')
        elif o == '-l':
            opts['do_list'] = True
        else:
            sys.stderr.write("Unknown option: %s\n", o)
            sys.exit(1)

    reactor.callWhenRunning(start)
    reactor.run()

if __name__ == '__main__':
    main()
