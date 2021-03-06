# -*- coding: iso-8859-1 -*-
# -----------------------------------------------------------------------------
# guide.py - EPG Guide
# -----------------------------------------------------------------------------
# $Id$
# -----------------------------------------------------------------------------
# kaa.epg - EPG Database
# Copyright (C) 2004-2008 Jason Tackaberry, Dirk Meyer, Rob Shortt
#
# First Edition: Jason Tackaberry <tack@sault.org>
#
# Please see the file AUTHORS for a complete list of authors.
#
# This library is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version
# 2.1 as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301 USA
#
# -----------------------------------------------------------------------------

__all__ = [ 'Guide' ]

# python imports
import os
import logging

# kaa imports
import kaa
from kaa.db import *
from kaa.utils import property
import kaa.dateutils

# kaa.epg imports
from channel import Channel
from program import Program
from util import cmp_channel, EPGError

# get logging object
log = logging.getLogger('epg')

class Guide(kaa.Object):
    """
    EPG guide with db access.
    """
    __kaasignals__ = {
        'program-retrieved':
            '''
            Emitted when a program is retrieve from the database.

            .. describe:: def callback(row, extra_info, ...)

               :param row: the program that has been retrieved from the database.
               :type row: dictionary type object

               :param extra_info: dictionary object to which to add additional info.
               :type extra_info: dictionary object

            Callbacks can add additional information to the program by adding
            keys to the extra_info dictionary.
            '''
    }


    def __init__(self, database):
        super(Guide, self).__init__()
        db_dir = os.path.dirname(database)
        if db_dir and not os.path.isdir(db_dir):
            os.makedirs(db_dir)
        self._db = Database(database)
        # create the db and register objects
        self._db.register_inverted_index('keywords', min = 2, max = 30)
        self._db.register_inverted_index('genres', min = 3, max = 30)
        self._db.register_object_type_attrs("channel",
            tuner_id = (list, ATTR_SIMPLE),
            name = (unicode, ATTR_SEARCHABLE),
            long_name = (unicode, ATTR_SEARCHABLE),
        )
        self._db.register_object_type_attrs("program",
            [ ("start", "stop") ],
            title = (unicode, ATTR_SEARCHABLE | ATTR_INVERTED_INDEX, 'keywords'),
            desc = (unicode, ATTR_SEARCHABLE | ATTR_INVERTED_INDEX, 'keywords'),
            # Program start time as a unix timestamp in UTC
            start = (int, ATTR_SEARCHABLE),
            # Program end time as a unix timestamp in UTC
            stop = (int, ATTR_SEARCHABLE),
            # Optional episode number or identifier (freeform string)
            episode = (unicode, ATTR_SIMPLE),
            # Optional program subtitle
            subtitle = (unicode, ATTR_SIMPLE | ATTR_INVERTED_INDEX, 'keywords'),
            # List of genres
            genres = (list, ATTR_SIMPLE | ATTR_INVERTED_INDEX, 'genres'),
            # FIXME: no idea what this is, it's used by epgdata backend.
            category = (unicode, ATTR_SEARCHABLE),
            # Original air date of the program.
            date = (int, ATTR_SEARCHABLE),
            # For movies, the year.
            year = (int, ATTR_SEARCHABLE),
            # Rating (could be TV, MPAA, etc.).  Freeform string.
            rating = (unicode, ATTR_SIMPLE),
            # List of unicode strings indicating any program advisors (violence, etc)
            advisories = (list, ATTR_SIMPLE),
            # A critical rating for the show/film.  Should be out of 4.0.
            score = (float, ATTR_SEARCHABLE),
            # Bitmask for the kaa.epg.Program.FLAG_* constants
            flags = (int, ATTR_SEARCHABLE),
            # List of credits (type, name, role)
            credits = (list, ATTR_SIMPLE)
        )
        self._sync()


    def _sync(self):
        """
        Sync cached guide data from database.
        """
        # Load some basic information from the db, stored as metadata during last update.
        self._max_program_length = int(self._db.get_metadata('kaa.epg::max_program_length', 0))
        self._num_programs = int(self._db.get_metadata('kaa.epg::num_programs', 0))

        # Build channel mappings (keyed by name, db id, and tuner id)
        self._channels_by_name = {}
        self._channels_by_db_id = {}
        self._channels_by_tuner_id = {}
        for objrow in self._db.query(type = "channel"):
            chan = Channel(objrow)
            self._channels_by_name[chan.name] = chan
            self._channels_by_db_id[chan.db_id] = chan
            for t in chan.tuner_id:
                if self._channels_by_tuner_id.has_key(t):
                    log.warning('loading channel %s with tuner_id %s already claimed by channel %s',
                                chan.name, t, self._channels_by_tuner_id[t].name)
                else:
                    self._channels_by_tuner_id[t] = chan

    @kaa.coroutine()
    def search(self, channel=None, time=None, cls=Program, **kwargs):
        """
        Search the EPG for programs.

        :param channel: Indicates the channel or channels within which to search
                        for programs.  Channels are specified by :class:`~kaa.epg.Channel`
                        objects.  If None is specified, all channels are searched.
        :type channel: :class:`~kaa.epg.Channel` object, list, or None
        :param time: Specifies a point in time or range of times.  If a range is
                     specified, a 2-tuple is used representing (start, stop).
                     Times can be specified as UNIX timestamps (seconds since
                     epoch UTC), or as Python datetime objects.  If a naive
                     datetime object is given (i.e. no tzinfo assigned), it is
                     treated as localtime.  A stop time of 0 means infinity. If
                     any part of the program's runtime intersects with the time
                     or time range provided, it matches.
        :type time: int or float, datetime, or 2-tuple of previous
        :param cls: Class used for program results.  The default is to return
                    :class:`~kaa.epg.Program` objects.  If None is given,
                    the raw query data (from kaa.db) is returned.
        :return: a list of :class:`~kaa.epg.Program` objects (or raw database
                 rows if ``cls=None``) matching the search criteria.


        Keyword arguments corresponding to the searchable columns of the
        underlying kaa.db database can also be specified.  They are:

            * ``title``: the title of the program (unicode)
            * ``desc``: the program's full description (unicode)
            * ``start``: program start time as unix timestamp (seconds since
                         epoch in UTC); you probably want to use the time kwarg
                         instead.
            * ``stop``: program start time as unix timestamp; you probably want
                        to use the time kwarg instead.
            * ``genres``: one or more genres (unicode or list of unicode); all
                          possible genres currently in the EPG can be gotten
                          using :meth:`~Guide.get_genres`.
            * ``date``: Original air date of the program, expressed as a UNIX
                        timestamp (int).
            * ``year``: for movies, the year of release (int).
            * ``score``: the critical rating for the program, especially for
                         movies (float); out of 4.0.
            * ``keywords``: one or more keywords which match against the program
                            title, description, and subtitle.

        With the exception of ``keywords`` and ``genres``, a :class:`~kaa.db.QExpr`
        object can be used with any of the above kwargs.
        """
        if channel is not None:
            if isinstance(channel, Channel):
                kwargs["parent"] = "channel", channel.db_id
            if isinstance(channel, (tuple, list)):
                kwargs["parent"] = [ ("channel", c.db_id) for c in channel ]

        if time is not None:
            # Find all programs currently playing at (or within) the given
            # time(s).  We push in the boundaries by 1 second as a heuristic to
            # prevent duplicates if the given time occurs on a boundary between
            # 2 programs.  e.g. if program A ends at 15:00 and B starts at 15:00,
            # searching for start=15:00 should return B and not A.
            if isinstance(time, (tuple, list)):
                start, stop = to_timestamp(time[0]) + 1, to_timestamp(time[1]) - 1
            else:
                start = stop = to_timestamp(time) + 1
            
            if stop > 0:
                kwargs["start"] = QExpr("range", (int(start) - self._max_program_length, int(stop)))
                kwargs["stop"]  = QExpr(">=", int(start))
            else:
                kwargs["start"] = QExpr(">=", (int(start) - self._max_program_length))

        query_data = self._db.query(type='program', **kwargs)
        # Convert raw search result data
        if kwargs.get('attrs'):
            attrs = kwargs.get('attrs')
            def combine_attrs(row):
                return [ row.get(a) for a in attrs ]
            [ combine_attrs(row) for row in query_data ]

        if len(self.signals['program-retrieved']):
            extra_data = []
            for row in query_data:
                extra_info = {}
                self.signals['program-retrieved'].emit(row, extra_info)
                extra_data.append(extra_info)
        else:
            extra_data = None

        if cls is None:
            # return raw data:
            yield query_data, extra_data

        # Convert raw search result data from the server into python objects.
        yield self._rows_to_programs(cls, query_data, extra_data)

    def _rows_to_programs(self, cls, query_data, extra_data):
        results = []
        for i,row in enumerate(query_data):
            if row['parent_id'] in self._channels_by_db_id:
                channel = self._channels_by_db_id[row['parent_id']]
            else:
                continue
            results.append(cls(channel, row, extra_data[i] if extra_data else None))
        return results

    def new_channel(self, tuner_id=None, name=None, long_name=None):
        """
        Returns a channel object that is not associated with the EPG.
        This is useful for clients that have channels that do not appear
        in the EPG but wish to handle them anyway.
        """
        # require at least one field
        if not tuner_id and not name and not long_name:
            log.error('need at least one field to create a channel')
            return None
        if not name:
            # then there must be one of the others
            if tuner_id:
                name = tuner_id[0]
            else:
                name = long_name
        if not long_name:
            # then there must be one of the others
            if name:
                long_name = name
            elif tuner_id:
                long_name = tuner_id[0]
        return Channel(tuner_id, name, long_name)

    def get_channel(self, name):
        """
        Get channel by name.
        """
        return self._channels_by_name.get(name)

    def get_channel_by_tuner_id(self, tuner_id):
        """
        Get channel by tuner id.
        """
        return self._channels_by_tuner_id.get(tuner_id)

    def get_channels(self, sort=False):
        """
        Get all channels
        """
        if sort:
            channels = self._channels_by_name.values()[:]
            channels.sort(lambda a, b: cmp(a.name, b.name))
            channels.sort(lambda a, b: cmp_channel(a, b))
            return channels
        return self._channels_by_name.values()


    @kaa.coroutine()
    def update(self, backend = None, *args, **kwargs):
        """
        Update the database
        """
        import sources
        yield sources.update(self._db, backend, *args, **kwargs)
        kaa.MainThreadCallable(self._sync)()


    @kaa.coroutine()
    def get_keywords(self, associated=None, prefix=None):
        """
        Retrieves a list of keywords in the database.
        """
        yield self._db.get_inverted_index_terms('keywords', associated, prefix)


    @kaa.coroutine()
    def get_genres(self, associated=None, prefix=None):
        """
        Retrieves a list of genres in the database.
        """
        yield self._db.get_inverted_index_terms('genres', associated, prefix)

    @property
    def num_programs(self):
        return self._num_programs

def to_timestamp(dt):
    """
    Converts a time to a unix timestamp (seconds since epoch UTC)
    """
    if isinstance(dt, (int, float, long)):
        return dt
    return kaa.dateutils.to_timestamp(dt)
