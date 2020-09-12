import io
import operator
import os
import stat
import time
from urllib.parse import urljoin

import cachetools
import refuse.high as fuse
import requests
from logzero import logger

import emojifs.utils as utils

# Yes, Mattermost supports JPEG emojis 🥴
MM_FILE_SUFFIX = {
    'image/gif': '.gif',
    'image/jpeg': '.jpg',
    'image/png': '.png',
}


class Mattermost(fuse.LoggingMixIn, fuse.Operations):
    """A FUSE filesystem implementation for a Mattermost instance's emojis."""

    def __init__(self, base_url: str, token: str):
        self._base_url = base_url
        self._write_buffers = {}

        self._emojis_cache = cachetools.TTLCache(maxsize=1, ttl=600)
        self._content_type_cache = cachetools.TTLCache(maxsize=1000, ttl=600)
        
        self._session = requests.Session()
        utils.set_user_agent(self._session.headers)
        self._session.headers['Authorization'] = f'Bearer {token}'

        j = self._request('GET', 'api/v4/users/me').json()
        logger.info('👍 Logged into Mattermost instance at %s as %s', base_url, j['username'])

    def _request(self, http_method, urlfrag, **kwargs):
        url = urljoin(self._base_url, urlfrag)
        # TODO: rate limit handling
        resp = self._session.request(http_method, url, **kwargs)
        return resp

    @cachetools.cachedmethod(operator.attrgetter('_emojis_cache'))
    def _get_all_emojis(self):
        all_emojis = {}

        page = 0
        while True:
            r = self._request('GET', f'api/v4/emoji?page={page}&per_page=100&sort=name')
            j = r.json()
            all_emojis.update({e['name']: e for e in j})
            if(len(j) < 100):
                break
            page += 1

        return all_emojis

    def _emoji_url(self, emoji_id: str) -> str:
        return urljoin(self._base_url, f'api/v4/emoji/{emoji_id}/image')

    @cachetools.cachedmethod(operator.attrgetter('_content_type_cache'))
    def _emoji_content_type(self, emoji_id: str) -> str:
        r = self._request('HEAD', self._emoji_url(emoji_id))
        content_type = r.headers.get("Content-Type")
        if not content_type:
            raise ValueError('Cannot infer filetype of emoji with no Content-Type')
        return content_type
        
    def _emoji_filename(self, e) -> str:
        suffix = MM_FILE_SUFFIX.get(self._emoji_content_type(e['id']))
        if not suffix:
            raise ValueError(f"Unsupported emoji Content-Type '{content_type}'")
        return f"{e['name']}{suffix}"

    @staticmethod
    def _emoji_name_from_path(path: str) -> str:
        name, _ = os.path.splitext(os.path.basename(path))
        return name

    def readdir(self, path, fh=None):
        rv = ['.', '..']
        if path != '/':
            return rv

        for e in self._get_all_emojis().values():
            try:
                rv.append(self._emoji_filename(e))
            except ValueError as error:
                logger.warn('Failed to determine filename for %r (%s)', e['name'], e['id'])

        return rv

    def getattr(self, path, fh):
        emojis = self._get_all_emojis()

        if path == '/':
            return dict(
                st_mode=stat.S_IFDIR | 0o555 | stat.S_IWUSR,
                st_mtime=max([e['update_at'] for e in emojis.values()]) / 1000,
                st_ctime=min([e['create_at'] for e in emojis.values()]) / 1000,
                st_atime=time.time(),
                st_nlink=2,
                st_uid=utils.getuid(),
                st_gid=utils.getgid(),
            )

        e = emojis.get(self._emoji_name_from_path(path))
        if not e:
            raise fuse.FuseOSError(errno.ENOENT)

        return dict(
            st_mode=stat.S_IFREG | 0o444,
            st_mtime=e['update_at'] / 1000,
            st_ctime=e['create_at'] / 1000,
            st_atime=time.time(),
            st_nlink=1,
            st_uid=utils.getuid(),
            st_gid=utils.getuid(),
            st_size=1024*1024,
        )

    def read(self, path, size, offset, fh):
        if path in self._write_buffers:
            b = self._write_buffers[path]
            b.seek(offset)
            return b.read(size)

        emojis = self._get_all_emojis()
        e = emojis.get(self._emoji_name_from_path(path))
        if not e:
            raise fuse.FuseOSError(errno.ENOENT)
        b = utils.get_emoji_bytes(self._emoji_url(e['id']), session=self._session)
        return b[offset:offset+size]

