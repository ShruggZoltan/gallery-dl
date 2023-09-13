# -*- coding: utf-8 -*-

# Copyright 2021-2023 Mike Fährmann
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

"""Extractors for https://kemono.party/"""

from .common import Extractor, Message
from .. import text, exception
from ..cache import cache
import itertools
import re

BASE_PATTERN = r"(?:https?://)?(?:www\.|beta\.)?(kemono|coomer)\.(party|su)"
USER_PATTERN = BASE_PATTERN + r"/([^/?#]+)/user/([^/?#]+)"
HASH_PATTERN = r"/[0-9a-f]{2}/[0-9a-f]{2}/([0-9a-f]{64})"


class KemonopartyExtractor(Extractor):
    """Base class for kemonoparty extractors"""
    category = "kemonoparty"
    root = "https://kemono.party"
    directory_fmt = ("{category}", "{service}", "{user}")
    filename_fmt = "{id}_{title}_{num:>02}_{filename[:180]}.{extension}"
    archive_fmt = "{service}_{user}_{id}_{num}"
    cookies_domain = ".kemono.party"

    def __init__(self, match):
        domain = match.group(1)
        tld = match.group(2)
        self.category = domain + "party"
        self.root = text.root_from_url(match.group(0))
        self.cookies_domain = ".{}.{}".format(domain, tld)
        Extractor.__init__(self, match)

    def _init(self):
        self.session.headers["Referer"] = self.root + "/"
        self._prepare_ddosguard_cookies()
        self._find_inline = re.compile(
            r'src="(?:https?://(?:kemono|coomer)\.(?:party|su))?(/inline/[^"]+'
            r'|/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.[^"]+)').findall

    def items(self):
        find_hash = re.compile(HASH_PATTERN).match
        generators = self._build_file_generators(self.config("files"))
        duplicates = self.config("duplicates")
        comments = self.config("comments")
        username = dms = None

        # prevent files from being sent with gzip compression
        headers = {"Accept-Encoding": "identity"}

        if self.config("metadata"):
            username = text.unescape(text.extract(
                self.request(self.user_url).text,
                '<meta name="artist_name" content="', '"')[0])
        if self.config("dms"):
            dms = True

        posts = self.posts()
        max_posts = self.config("max-posts")
        if max_posts:
            posts = itertools.islice(posts, max_posts)

        for post in posts:

            headers["Referer"] = "{}/{}/user/{}/post/{}".format(
                self.root, post["service"], post["user"], post["id"])
            post["_http_headers"] = headers
            post["date"] = text.parse_datetime(
                post["published"] or post["added"],
                "%a, %d %b %Y %H:%M:%S %Z")
            if username:
                post["username"] = username
            if comments:
                post["comments"] = self._extract_comments(post)
            if dms is not None:
                if dms is True:
                    dms = self._extract_dms(post)
                post["dms"] = dms

            files = []
            hashes = set()

            for file in itertools.chain.from_iterable(
                    g(post) for g in generators):
                url = file["path"]

                match = find_hash(url)
                if match:
                    file["hash"] = hash = match.group(1)
                    if not duplicates:
                        if hash in hashes:
                            self.log.debug("Skipping %s (duplicate)", url)
                            continue
                        hashes.add(hash)
                else:
                    file["hash"] = ""

                files.append(file)

            post["count"] = len(files)
            yield Message.Directory, post

            for post["num"], file in enumerate(files, 1):
                post["_http_validate"] = None
                post["hash"] = file["hash"]
                post["type"] = file["type"]
                url = file["path"]

                text.nameext_from_url(file.get("name", url), post)
                ext = text.ext_from_url(url)
                if not post["extension"]:
                    post["extension"] = ext
                elif ext == "txt" and post["extension"] != "txt":
                    post["_http_validate"] = _validate

                if url[0] == "/":
                    url = self.root + "/data" + url
                elif url.startswith(self.root):
                    url = self.root + "/data" + url[20:]
                yield Message.Url, url, post

    def login(self):
        username, password = self._get_auth_info()
        if username:
            self.cookies_update(self._login_impl(
                (username, self.cookies_domain), password))

    @cache(maxage=28*24*3600, keyarg=1)
    def _login_impl(self, username, password):
        username = username[0]
        self.log.info("Logging in as %s", username)

        url = self.root + "/account/login"
        data = {"username": username, "password": password}

        response = self.request(url, method="POST", data=data)
        if response.url.endswith("/account/login") and \
                "Username or password is incorrect" in response.text:
            raise exception.AuthenticationError()

        return {c.name: c.value for c in response.history[0].cookies}

    def _file(self, post):
        file = post["file"]
        if not file:
            return ()
        file["type"] = "file"
        return (file,)

    def _attachments(self, post):
        for attachment in post["attachments"]:
            attachment["type"] = "attachment"
        return post["attachments"]

    def _inline(self, post):
        for path in self._find_inline(post["content"] or ""):
            yield {"path": path, "name": path, "type": "inline"}

    def _build_file_generators(self, filetypes):
        if filetypes is None:
            return (self._attachments, self._file, self._inline)
        genmap = {
            "file"       : self._file,
            "attachments": self._attachments,
            "inline"     : self._inline,
        }
        if isinstance(filetypes, str):
            filetypes = filetypes.split(",")
        return [genmap[ft] for ft in filetypes]

    def _extract_comments(self, post):
        url = "{}/{}/user/{}/post/{}".format(
            self.root, post["service"], post["user"], post["id"])
        page = self.request(url).text

        comments = []
        for comment in text.extract_iter(page, "<article", "</article>"):
            extr = text.extract_from(comment)
            cid = extr('id="', '"')
            comments.append({
                "id"  : cid,
                "user": extr('href="#' + cid + '"', '</').strip(" \n\r>"),
                "body": extr(
                    '<section class="comment__body">', '</section>').strip(),
                "date": extr('datetime="', '"'),
            })
        return comments

    def _extract_dms(self, post):
        url = "{}/{}/user/{}/dms".format(
            self.root, post["service"], post["user"])
        page = self.request(url).text

        dms = []
        for dm in text.extract_iter(page, "<article", "</article>"):
            dms.append({
                "body": text.unescape(text.extract(
                    dm, "<pre>", "</pre></",
                )[0].strip()),
                "date": text.extr(dm, 'datetime="', '"'),
            })
        return dms


def _validate(response):
    return (response.headers["content-length"] != "9" or
            response.content != b"not found")


class KemonopartyUserExtractor(KemonopartyExtractor):
    """Extractor for all posts from a kemono.party user listing"""
    subcategory = "user"
    pattern = USER_PATTERN + r"/?(?:\?o=(\d+))?(?:$|[?#])"
    example = "https://kemono.party/SERVICE/user/12345"

    def __init__(self, match):
        _, _, service, user_id, offset = match.groups()
        self.subcategory = service
        KemonopartyExtractor.__init__(self, match)
        self.api_url = "{}/api/{}/user/{}".format(self.root, service, user_id)
        self.user_url = "{}/{}/user/{}".format(self.root, service, user_id)
        self.offset = text.parse_int(offset)

    def posts(self):
        url = self.api_url
        params = {"o": self.offset}

        while True:
            posts = self.request(url, params=params).json()
            yield from posts

            cnt = len(posts)
            if cnt < 25:
                return
            params["o"] += cnt


class KemonopartyPostExtractor(KemonopartyExtractor):
    """Extractor for a single kemono.party post"""
    subcategory = "post"
    pattern = USER_PATTERN + r"/post/([^/?#]+)"
    example = "https://kemono.party/SERVICE/user/12345/post/12345"

    def __init__(self, match):
        _, _, service, user_id, post_id = match.groups()
        self.subcategory = service
        KemonopartyExtractor.__init__(self, match)
        self.api_url = "{}/api/{}/user/{}/post/{}".format(
            self.root, service, user_id, post_id)
        self.user_url = "{}/{}/user/{}".format(self.root, service, user_id)

    def posts(self):
        posts = self.request(self.api_url).json()
        return (posts[0],) if len(posts) > 1 else posts


class KemonopartyDiscordExtractor(KemonopartyExtractor):
    """Extractor for kemono.party discord servers"""
    subcategory = "discord"
    directory_fmt = ("{category}", "discord", "{server}",
                     "{channel_name|channel}")
    filename_fmt = "{id}_{num:>02}_{filename}.{extension}"
    archive_fmt = "discord_{server}_{id}_{num}"
    pattern = BASE_PATTERN + r"/discord/server/(\d+)(?:/channel/(\d+))?#(.*)"
    example = "https://kemono.party/discard/server/12345/channel/12345"

    def __init__(self, match):
        KemonopartyExtractor.__init__(self, match)
        _, _, self.server, self.channel, self.channel_name = match.groups()

    def items(self):
        self._prepare_ddosguard_cookies()

        find_inline = re.compile(
            r"https?://(?:cdn\.discordapp.com|media\.discordapp\.net)"
            r"(/[A-Za-z0-9-._~:/?#\[\]@!$&'()*+,;%=]+)").findall
        find_hash = re.compile(HASH_PATTERN).match

        posts = self.posts()
        max_posts = self.config("max-posts")
        if max_posts:
            posts = itertools.islice(posts, max_posts)

        for post in posts:
            files = []
            append = files.append
            for attachment in post["attachments"]:
                match = find_hash(attachment["path"])
                attachment["hash"] = match.group(1) if match else ""
                attachment["type"] = "attachment"
                append(attachment)
            for path in find_inline(post["content"] or ""):
                append({"path": "https://cdn.discordapp.com" + path,
                        "name": path, "type": "inline", "hash": ""})

            post["channel_name"] = self.channel_name
            post["date"] = text.parse_datetime(
                post["published"], "%a, %d %b %Y %H:%M:%S %Z")
            post["count"] = len(files)
            yield Message.Directory, post

            for post["num"], file in enumerate(files, 1):
                post["hash"] = file["hash"]
                post["type"] = file["type"]
                url = file["path"]

                text.nameext_from_url(file.get("name", url), post)
                if not post["extension"]:
                    post["extension"] = text.ext_from_url(url)

                if url[0] == "/":
                    url = self.root + "/data" + url
                elif url.startswith(self.root):
                    url = self.root + "/data" + url[20:]
                yield Message.Url, url, post

    def posts(self):
        if self.channel is None:
            url = "{}/api/discord/channels/lookup?q={}".format(
                self.root, self.server)
            for channel in self.request(url).json():
                if channel["name"] == self.channel_name:
                    self.channel = channel["id"]
                    break
            else:
                raise exception.NotFoundError("channel")

        url = "{}/api/discord/channel/{}".format(self.root, self.channel)
        params = {"skip": 0}

        while True:
            posts = self.request(url, params=params).json()
            yield from posts

            cnt = len(posts)
            if cnt < 25:
                break
            params["skip"] += cnt


class KemonopartyDiscordServerExtractor(KemonopartyExtractor):
    subcategory = "discord-server"
    pattern = BASE_PATTERN + r"/discord/server/(\d+)$"
    example = "https://kemono.party/discard/server/12345"

    def __init__(self, match):
        KemonopartyExtractor.__init__(self, match)
        self.server = match.group(3)

    def items(self):
        url = "{}/api/discord/channels/lookup?q={}".format(
            self.root, self.server)
        channels = self.request(url).json()

        for channel in channels:
            url = "{}/discord/server/{}/channel/{}#{}".format(
                self.root, self.server, channel["id"], channel["name"])
            channel["_extractor"] = KemonopartyDiscordExtractor
            yield Message.Queue, url, channel


class KemonopartyFavoriteExtractor(KemonopartyExtractor):
    """Extractor for kemono.party favorites"""
    subcategory = "favorite"
    pattern = BASE_PATTERN + r"/favorites(?:/?\?([^#]+))?"
    example = "https://kemono.party/favorites"

    def __init__(self, match):
        KemonopartyExtractor.__init__(self, match)
        self.favorites = (text.parse_query(match.group(3)).get("type") or
                          self.config("favorites") or
                          "artist")

    def items(self):
        self._prepare_ddosguard_cookies()
        self.login()

        if self.favorites == "artist":
            users = self.request(
                self.root + "/api/favorites?type=artist").json()
            for user in users:
                user["_extractor"] = KemonopartyUserExtractor
                url = "{}/{}/user/{}".format(
                    self.root, user["service"], user["id"])
                yield Message.Queue, url, user

        elif self.favorites == "post":
            posts = self.request(
                self.root + "/api/favorites?type=post").json()
            for post in posts:
                post["_extractor"] = KemonopartyPostExtractor
                url = "{}/{}/user/{}/post/{}".format(
                    self.root, post["service"], post["user"], post["id"])
                yield Message.Queue, url, post
