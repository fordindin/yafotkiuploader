#!/usr/bin/env python
# -*- coding: utf-8 -*-

####
# 05/2008 Alexander Atemenko <svetlyak.40wt@gmail.com>
#
# Special thanks to:
# Grigory Bakunov <bobuk@justos.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import MultipartPostHandler, urllib2, cookielib
import os, sys, re
from hashlib import md5
import urllib
import logging
import time

from pdb import set_trace
from xml.dom import minidom
from StringIO import StringIO

logging.basicConfig(level=logging.WARNING)

VERSION = '0.2.7'

try:
    from pyexiv2 import ImageMetadata
except ImportError:
    logging.getLogger('YaFotki.pre_init').warning('can\'t find python-pyexiv2 library, exif extraction will be disabled.')
    ImageMetadata = None

ALBUMS_URL= 'http://fotki.yandex.ru/users/%s/albums/'
UPLOAD_URL = 'http://up.fotki.yandex.ru/upload'

class FileNotFound(RuntimeWarning): pass
class NoPasswdOrCallback(RuntimeError): pass
class AuthError(RuntimeError): pass

class Uploader(object):
    def __init__(
            self,
            username,
            password = None,
            password_callback = None,
            cookies_cache = None):

        self.username = username
        self.password = password
        self.password_callback = password_callback
        self.cookies = None
        self.cookies_cache = cookies_cache and os.path.expanduser(cookies_cache)

    def get_albums(self):
        logger = logging.getLogger('YaFotki.get_albums')
        opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(self.cookies), MultipartPostHandler.MultipartPostHandler)

        fake_file = StringIO('<?xml version="1.0" encoding="utf-8"?><client-upload name="get-albums"/>')

        params = {
            'query-type' : 'photo-command',
            'command-xml' : fake_file
        }

        try:
            data = opener.open(UPLOAD_URL, params).read()
            xml = minidom.parseString(data)
            albums = []
            for album in xml.getElementsByTagName('album'):
                id = album.attributes['id'].value
                title = album.getElementsByTagName('title')[0].firstChild.nodeValue
                albums.append( (id, title) )
            albums.sort()
            return albums

        except urllib2.URLError, err:
            logger.error(err)
            raise
        return []

    def post(self, img, album):
        if not os.path.exists(img):
            raise FileNotFound('Can\'t find image %s on the disk' % img)

        logger = logging.getLogger('YaFotki.post')
        opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(self.cookies), MultipartPostHandler.MultipartPostHandler)

        filename = os.path.split(img)[-1]
        tags = ''
        title = filename
        description = ''

        if ImageMetadata:
            exif = ImageMetadata(img)
            try: tags = ','.join([tag.decode('utf8') for tag in exif['Iptc.Application2.Keywords']])
            except KeyError: pass
            try: title = exif['Iptc.Application2.ObjectName'].decode('utf8')
            except KeyError: pass
            try: description = exif['Exif.Image.ImageDescription'].decode('utf8') or exif['Iptc.Application2.Caption'].decode('utf8')
            except KeyError: pass

        source = open(img, 'rb')
        source.seek(0, 2)
        file_size = source.tell()
        piece_size = 64000

        sid = str(int(time.time()))
        source.seek(0)
        hash = md5(source.read()).hexdigest()

        logger.debug('md5hash: %s, sid: %s, file-size: %s, piece-size: %s' % (hash, sid, file_size, piece_size))
        logger.debug('title: %s, description: %s tags: %s' % (title, description, tags))

        logger.debug('photo-start')
        # START
        fake_file = StringIO((u'<?xml version="1.0" encoding="utf-8"?><client-upload md5="%(md5)s" cookie="%(md5)s%(sid)s"><filename>%(filename)s</filename><title>%(title)s</title><description>%(description)s</description><albumId>%(album)s</albumId><copyright>0</copyright><tags>%(tags)s</tags></client-upload>' % {
            'md5': hash,
            'sid': sid,
            'filename': filename,
            'title': title,
            'album': album,
            'tags': tags,
            'description': description,
        }).encode('utf8'))

        params = {
            'query-type': 'photo-start',
            'file-size': str(file_size),
            'piece-size': str(piece_size),
            'checksum': hash,
            'client-xml': fake_file,
        }

        try:
            data = opener.open(UPLOAD_URL, params).read()
            logger.debug(data)
            response = minidom.parseString(data).firstChild
            a = response.attributes
            if a['status'].value == 'error':
                if a['exception'].value == '3':
                    logger.error('Album with id %s does not exist.' % album)
                else:
                    logger.error('Error during upload, with code %s' % a['exception'].value)
                sys.exit(1)
            upload_cookie = str(a['cookie'].value)
        except urllib2.URLError, err:
            logger.error(err)
            logger.error(err.read())
            return err

        source.seek(0)
        while 1:
            offset = source.tell()
            data = source.read(piece_size)
            if not data:
                break

            logger.debug('photo-piece, offset=%s' % offset)

            piece = StringIO(data)

            params = {
                'query-type': 'photo-piece',
                'cookie': upload_cookie,
                'offset': str(offset),
                'fragment': piece,
            }

            try:
                data = opener.open(UPLOAD_URL, params).read()
                logger.debug(data)
            except urllib2.URLError, err:
                logger.error(err)
                logger.error(err.read())
                return err

        logger.debug('photo-checksum')

        params = {
            'query-type': 'photo-checksum',
            'cookie': upload_cookie,
            'size': str(piece_size),
        }

        try:
            data = opener.open(UPLOAD_URL, params).read()
            logger.debug(data)
        except urllib2.URLError, err:
            logger.error(err)
            logger.error(err.read())

            logger.debug('check-upload')
            fake_file = StringIO('<?xml version="1.0" encoding="utf-8"?><client-upload name="check-upload" cookie="%(md5)s%(sid)s" login="%(login)s"></client-upload>' % {
                'md5': hash,
                'sid': sid,
                'login': self.username,
            })

            params = {
                'query-type': 'photo-command',
                'command-xml': fake_file,
            }
            try:
                data = opener.open(UPLOAD_URL, params).read()
                logger.debug(data)
                response = minidom.parseString(data).firstChild
                if response.nodeName != 'images':
                    logger.error('error upload check: %s' % data)
                    sys.exit(1)
            except urllib2.URLError, err:
                logger.error(err)
                logger.error(err.read())
                return err

        logger.debug('photo-finish')

        params = {
            'query-type': 'photo-finish',
            'cookie': upload_cookie,
        }

        try:
            data = opener.open(UPLOAD_URL, params).read()
            logger.debug(data)

            response = minidom.parseString(data).firstChild
            if response.attributes['status'].value == 'error':
                logger.error('error during upload, with code %s' % response.attributes['exception'].value)
        except urllib2.URLError, err:
            logger.error(err)
            logger.error(err.read())

    def auth(self):
        logger = logging.getLogger('YaFotki.auth')
        cj = self.__create_auth_opener()
        for cookie in cj:
            if cookie.name == "yandex_login":
                if cookie.value == self.username:
                    logger.debug(cj)
                    self.cookies = cj
                    return True
                else:
                    if self.cookies_cache and os.path.exists(self.cookies_cache):
                        os.remove(self.cookies_cache)
        raise AuthError('Can\'t authorize as user %s with given password!' % self.username)


    def __create_auth_opener(self):
        cj = cookielib.LWPCookieJar()
        opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cj))
        if self.cookies_cache:
            if os.path.isfile(self.cookies_cache):
                cj.load(self.cookies_cache)
                cj.clear_expired_cookies()
                for ck in cj:
                    if ck.name == 'yandex_login' and ck.value == self.username:
                        logging.getLogger('YaFotki.auth').debug('Authorized by cookie')
                        return cj

        if self.password is None:
            if self.password_callback is not None:
                self.password = self.password_callback()
            else:
                raise NoPasswdOrCallback('Please, specify either password or password_callback.')

        logger = logging.getLogger('YaFotki.auth')
        logger.info('authorization as %s with password %s...' % (self.username, '*'* len(self.password)))
        logger.debug('real password is %s' % self.password)
        data = {
                'login': self.username,
                'passwd': self.password,
                'twoweeks':'yes'
                }
        opener.open("https://passport.yandex.ru/passport?mode=auth",
                           urllib.urlencode(data))
        if self.cookies_cache:
            cj.save(self.cookies_cache)
        return cj

