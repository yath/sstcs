#!/usr/bin/python
import codecs
import getopt
import logging
import os
from struct import unpack
import sys
import time

from twisted.internet import reactor
from twisted.python.failure import Failure
import twisted.web.client

from coherence.base import Coherence
from coherence.upnp.devices.control_point import ControlPoint

import coherence.extern.log.log as coherence_log

from xml.sax.saxutils import escape

# Channel list types to use as fallbacks if designated channel is not in
# current channel list type. 0x11..0x15 are favourites 1..5, 0x01 is all,
# 0x03 is TV, 0x04 is Radio, 0x06 is analogue. Dunno for the other numbers.
# And it's literally the string, 0xXY.
CL_TYPE_FALLBACKS = '0x11 0x12 0x13 0x14 0x15 0x03 0x04 0x06 0x01'.split(' ')

# Maps Coherence log levels to 'logging' log levels
COHERENCE_LOG_LEVEL_MAP = {
    'ERROR': logging.ERROR,
    'WARN' : logging.WARNING,
    'INFO' : logging.INFO,
    'DEBUG': logging.DEBUG,
    'LOG'  : logging.DEBUG,
}

# Log levels to colors.
try:
    from coherence.extern.log import termcolor
    t = termcolor.TerminalController()
    LOG_LEVEL_COLORS = {
        logging.CRITICAL: t.BOLD+t.MAGENTA,
        logging.ERROR   : t.BOLD+t.RED,
        logging.WARNING : t.BOLD+t.YELLOW,
        logging.INFO    : t.BOLD+t.GREEN,
        logging.DEBUG   : t.BOLD+t.BLUE,
    }
    LOG_LEVEL_COLOR_RESET = t.NORMAL
except ImportError:
    LOG_LEVEL_COLORS = {}
    LOG_LEVEL_COLOR_RESET = ''

# Global Logger for sstcs. Will be set in set_up_logging.
LOG = None

# Code to exit with at the end of the program
EXITCODE = 0

# Default options
opts = {
    'loglevels': 'info,coherence=critical',
    'devtype'  : 'urn:samsung.com:device:MainTVServer2:1',
    'channel'  : None,
    'do_list'  : False,
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

    log_str = 'FATAL ERROR: %s' % msg
    if failure:
        log_str += '\nAdditional information:\n%s' % str(failure)

    if LOG:
        LOG.critical(log_str)
    else:
        sys.stderr.write(log_str + '\n')

    if reactor.running:
        reactor.stop()

    global EXITCODE
    EXITCODE = 2

class LogFormatter(logging.Formatter):
    """Formatter for sstcs' log. Colors the log level and auto-grows columns."""

    def __init__(self, initial_widths={}):
        self.widths = initial_widths.copy()
        super(LogFormatter, self).__init__()

    def _get_padded_text(self, what, text):
        width = self.widths.get(what, 0)
        if len(text) > width:
            self.widths[what] = width = len(text)
        return '{text:{width}s}'.format(text=text, width=width)


    def format(self, record):
        colored_loglevel = (LOG_LEVEL_COLORS.get(record.levelno, '') +
                            self._get_padded_text('levelname', record.levelname) +
                            LOG_LEVEL_COLOR_RESET)
        formatted_time = '%s,%03d' % (time.strftime("%H:%M:%S", time.localtime(record.created)),
                                      record.msecs)

        try:
            message = record.message
        except AttributeError:
            message = record.getMessage()

        name = record.name

        # If it's a warning, PyWarningsFilter has probably set real_module
        # which we can use as name.
        if name == 'py.warnings' and hasattr(record, 'real_module'):
            name = record.real_module
        padded_name = self._get_padded_text('name', name)
        return '\n'.join("%s %s %s %s" % (formatted_time,
                                          padded_name,
                                          colored_loglevel,
                                          line) for line in message.split('\n'))



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

    LOG.debug('set_channel_returned: result=%r, fallbacks=%r, channel=%r', result,
        cl_type_fallbacks, channel)

    if result['Result'] == 'NOTOK_InvalidCh':
        try:
            cl_type = cl_type_fallbacks.pop(0)
        except IndexError:
            fatal('TV doesn\'t know how to switch to %s' % channel)
            return

        LOG.warning("channel %s not in current channel list, trying with %s",
                    channel, cl_type)
        set_main_tv_channel.call(ChannelListType=cl_type,
                                 SatelliteID=0,
                                 Channel=channel.as_xml()).\
                        addCallback(set_channel_returned, set_main_tv_channel,
                                    cl_type_fallbacks, channel)
    elif result['Result'] == 'OK':
        LOG.info('Channel switched.')
        reactor.stop()
        return
    else:
        fatal('TV reported back result %s, no idea what that is.' % result)
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

    LOG.debug('Parsed %d channels', len(channels))
    return channels


def got_channel_list(channel_list, cl_type, service):
    """Called when the channel list URL has been retrieved. Parses the channel list, looks
    for a matching channel and calls SetMainTVChannel with the passed cl_type (channel list
    type), unless opts['do_list'] is true, in which case it just prints the channels and
    terminates Twisted.

    Next: set_channel_returned, passing a list of fallback channel types and everything
    needed to reproduce the call to SetMainTVChannel for the fallback channel lists."""

    LOG.debug('got_channel_list: fetched %d bytes', len(channel_list))
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
        logging.info("More than one matching channel found (%s), picking first", matching_channels)
    channel_xml = matching_channels[0].as_xml()

    set_main_tv_channel = service.get_action('SetMainTVChannel')
    if not set_main_tv_channel:
        # FIXME retry, uh?
        fatal('Can\'t resolve SetMainTVChannel on TV, that\'s usually intermittent.')
        return

    LOG.debug('Calling SetMainTVChannel(ChannelListType=%r, SatelliteID=0, Channel=%r)',
        cl_type, channel_xml)

    set_main_tv_channel.call(ChannelListType=cl_type, SatelliteID=0, Channel=channel_xml).\
        addCallback(set_channel_returned, set_main_tv_channel, CL_TYPE_FALLBACKS[:], channel).\
        addErrback(fatal)


def got_channel_list_url(results, service):
    """Called when GetChannelListURL returns. Gets the URL referenced from the channel list and
    the channel list type. Next: got_channel_list(channel_list_type)."""

    LOG.debug('got_channel_list_url: %r', results)

    cl_type = results['ChannelListType']
    url     = results['ChannelListURL']

    LOG.debug('Current cl_type is %s, URL is %s. Fetching URL.',
        cl_type, url)
    twisted.web.client.getPage(url).addCallback(got_channel_list, cl_type, service).\
        addErrback(fatal)


def dev_found(device):
    """Called when a device was found and calls GetChannelListURL if the device matches and has
    the appropriate service. Next: got_channel_list_url(service)."""

    LOG.debug('Discovered device %r', device)
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
    LOG.debug('Found matching service %r', svc)

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

    LOG.debug('Calling GetChannelListURL')
    get_channel_list_url.call().addCallback(got_channel_list_url, svc).\
        addErrback(fatal)


def start():
    """Starts up Coherence and sets everything up. Next: dev_found()"""

    def _log_handler(level, obj, category, file, line, msg, *args):
        try:
            log_level = COHERENCE_LOG_LEVEL_MAP[coherence_log.getLevelName(level)]
        except KeyError:
            log_level = 'NOTSET'

        if category == 'coherence':
            logger_name = 'coherence.main'
        else:
            logger_name = 'coherence.%s' % category

        l = logging.getLogger(logger_name)
        l.log(log_level, msg, *args)

    coherence_log.addLogHandler(_log_handler)
    try:
        coherence_log.removeLimitedLogHandler(coherence_log.stderrHandler)
    except ValueError:
        pass

    c = Coherence({'logmode': 'none'})
    cp = ControlPoint(c, auto_client=[])
    LOG.debug('Coherence initialized, waiting for devices to be discovered...')
    cp.connect(dev_found, 'Coherence.UPnP.RootDevice.detection_completed')

class PyWarningsFilter(logging.Filter):
    """A filter for py.warnings which resolves the cause (module) of the
    warning and checks their logger whether we should log."""

    def __init__(self, *args, **kwargs):
        self.module_names_cache = {}
        super(PyWarningsFilter, self).__init__(*args, **kwargs)

    def _module_name_from_filename(self, filename):
        try:
            return self.module_names_cache[filename]
        except KeyError:
            pass

        # we can't use os.path.realpath as it resolves symlinks, which we
        # do not want, so normalize "as good as possible".
        normalize = lambda path: os.path.normcase(os.path.normpath(path))
        normalized_filename = normalize(filename)
        for modpath in sorted(sys.path, key=lambda e: len(e), reverse=True):
            normalized_modpath = normalize(modpath)+os.path.sep
            if normalized_filename.startswith(normalized_modpath):
                # we can safely strip at os.path.sep, normpath already replaced
                # altsep with sep
                rest = normalized_filename.replace(normalized_modpath, '', 1).split(os.path.sep)
                # strip python extension from filename
                rest[-1] = (os.path.splitext(rest[-1]))[0]
                if rest[-1] == '__init__':
                    rest.pop()  # drop __init__
                module_name = self.module_names_cache[filename] = '.'.join(rest)
                return module_name

        self.module_names_cache[filename] = None
        return None

    def filter(self, record):
        assert record.name == 'py.warnings'

        if not hasattr(record, 'real_module'):
            record.real_module = self._module_name_from_filename(record.pathname)

        return logging.getLogger(record.real_module).isEnabledFor(record.levelno)

def set_up_logging(levels_string):
    """Sets up logging and configures the log levels according to levels_string."""

    sh = logging.StreamHandler()
    sh.setFormatter(LogFormatter())
    logging.getLogger().addHandler(sh)

    logging.getLogger('py.warnings').addFilter(PyWarningsFilter())
    logging.captureWarnings(True)

    for level_string in levels_string.split(','):
        try:
            logger_name, level = level_string.split('=')
            logger = logging.getLogger(logger_name)
        except ValueError:
            logger = logging.getLogger()  # root logger
            level = level_string
        logger.setLevel(level.upper())

    global LOG
    LOG = logging.getLogger('sstcs')

def main():
    """Parse options, set everything up and start twisted.reactor. Next: start()"""

    # Force stdout to be utf-8 so we can actually pipe our output to grep. Argh.
    sys.stdout = codecs.getwriter('utf8')(sys.stdout)

    try:
        gopts, rest_ = getopt.getopt(sys.argv[1:], "L:t:c:l",
                                     ["loglevels=", "devtype=", "channel=", "list"])
    except getopt.GetoptError as err:
        print str(err)
        sys.exit(1)

    for o, a in gopts:
        if o in ['-L', '--loglevels']:
            opts['loglevels'] += ','+a
        elif o in ['-t', '--devtype']:
            opts['devtype'] = a
        elif o in ['-c', '--channel']:
            try:
                opts['channel'] = a.decode('utf-8')
            except UnicodeDecodeError:
                # any binary string is valid iso-8859-1, so it *should* never
                # raise an error
                opts['channel'] = a.decode('iso-8859-1')
        elif o in ['-l', '--list']:
            opts['do_list'] = True
        else:
            fatal('Unknown option: %s', o)
            return

    if not opts['channel'] and not opts['do_list']:
        fatal('Either -c or -l must be specified.')
        return

    set_up_logging(opts['loglevels'])

    reactor.callWhenRunning(start)
    reactor.run()

if __name__ == '__main__':
    main()

sys.exit(EXITCODE)
