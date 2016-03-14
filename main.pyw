#!/usr/bin/env python3

import feedparser
import re
import pickle
import urllib
from time import strftime

# ##############################################################################
# GLOBAL OPTIONS
# ##############################################################################
rss_feeds = [
    {
        "url": "http://rlsbb.com/category/movies/feed/",
        "title_regex": r"^(.+)\s+(\d{4})\s+(?!\d+\s)(.+)\s+([^\s-]+-[^\s]+)$",
        "genre_regex": r".*Genre:</strong>\s*([^<]+).*",
        "lang_regex":  r".*Audio:[^\n]*(English)"
    }
]

# cache entries
cache_file = "cached.dat"

# where to add torrents to?
rss_file_output = "generated.rss"

# the following is processed IN ORDER from left -> right
allowed_qualities = ["BDRip", "720p", "1080p"]

# minimum year
minimum_year = 2010

# banned genres
banned_genres = ["Horror", "Romance", "Sport", "Biography", "War"]

# minimum score on rotten tomatoes
min_rt_score = {
    "critics": 70,
    "users": 75
}

# torrent search url (will replace %%query%%)
torrent_search_url = "https://kat.cr/usearch/%%query%%/?rss=1"

# rotten tomatoes url
rotten_tomatoes_search_url = "http://www.rottentomatoes.com/search/?search=%%query%%"

# ##############################################################################
# END GLOBAL OPTIONS
# ##############################################################################

# Sanitise
allowed_qualities = [i.upper() for i in allowed_qualities]
banned_genres = [i.upper() for i in banned_genres]

class RSSFilmDownloader(object):
    def __init__(self, cache_file, rss_file_output, torrent_search_url, rotten_tomatoes_search_url):
        self.cache_file = cache_file
        self.rss_file_output = rss_file_output
        self.torrent_search_url = torrent_search_url
        self.rotten_tomatoes_search_url = rotten_tomatoes_search_url

    def load_cache(self):
        try:
            with open(self.cache_file, 'rb') as fh_cache_file:
                self.cached_entries = pickle.load(fh_cache_file)
        except FileNotFoundError:
            # no cache yet
            self.cached_entries = [ ]

        return self.cached_entries

    def parse_feed(self, rss_feeds, allowed_qualities, minimum_year, banned_genres, min_rt_score):
        # main loop
        self.load_cache()

        for rss in rss_feeds:
            parsed = feedparser.parse(rss['url'])
            title_regexp = re.compile(rss['title_regex'])
            genre_regexp = re.compile(rss['genre_regex'], re.S | re.I)
            lang_regexp  = re.compile(rss['lang_regex'], re.S | re.I)

            for entry in parsed['entries']:
                m = title_regexp.match(entry.title)
                if not m:
                    print("Error parsing entry title: \"%s\"" % entry.title)
                    continue
                title,year,quality,tag = m.groups()
                year = int(year)
                entry_stats = {
                    "source_url": rss['url'],
                    "title": title,
                    "year": year,
                    "quality": quality
                }

                # check in cache
                is_cached = False
                for cached_entry in self.cached_entries:
                    if cached_entry['title'].upper() == title.upper():
                        if cached_entry['year'] == year:
                            is_cached = True
                            break
                if is_cached: continue

                # debug
                print("Processing: \"%s\" (%d), Q=%s, Misc=%s" % (title,year,quality,tag))

                # Check quality
                cont = False
                quality = quality.upper()
                for q in allowed_qualities:
                    if quality in q or q in quality:
                        cont = True
                        break
                if not cont:
                    print("Skipping: Wrong quality")
                    self.mark_processed(entry_stats)
                    continue

                # Check year
                if year < minimum_year:
                    print("Skipping: Wrong year")
                    self.mark_processed(entry_stats)
                    continue

                # Check genre
                cont = True
                genre_match = genre_regexp.match(entry.content[0].value)
                if genre_match:
                    for genre in re.split(r"\s*\|?\s*", genre_match.group(1)):
                        if genre.upper() in banned_genres:
                            print("Skipping: Banned Genre (%s)" % genre)
                            cont = False
                else:
                    print("WARNING: Could not find genre for \"%s\"" % title)
                if not cont:
                    self.mark_processed(entry_stats)
                    continue

                # Check language
                if not lang_regexp.match(entry.content[0].value):
                    print("Skipping: Wrong language")
                    self.mark_processed(entry_stats)
                    continue

                # Check Rotten Tomatoes
                scores = self.check_rotten_tomatoes("%s %d" % (title, year))
                cont = True
                for key in scores:
                    if scores[key] == -1:
                        print("Skipping: Could not find RT %s score" % key)
                        cont = False
                        break
                    elif scores[key] < min_rt_score[key]:
                        print("Skipping: Wrong RT %s score (%d%%)" % (key, scores[key]))
                        cont = False
                        break

                if not cont:
                    self.mark_processed(entry_stats)
                    continue

                # Find torrent
                release_name_regexp = re.compile(r".*/([^/]+)\.(?:[^\.]+)$")
                release_name = None
                for link in entry.links:
                    if "video" in link.type.lower():
                        m = release_name_regexp.match(link.href)
                        if m:
                            release_name = m.group(1)
                            print("Searching: \"%s\" -> %s" % (title, release_name))
                            break

                torrent_url = None
                if release_name:
                    torrent_url = self.check_torrent(release_name)
                if release_name and not torrent_url:
                    release_name = re.sub(r"^.*?(%s)" % year, title, release_name)
                    torrent_url = self.check_torrent(release_name)
                if not torrent_url:
                    release_name = "%s %d %s" % (title, year, quality)
                    torrent_url = self.check_torrent(release_name)
                    if not torrent_url:
                        print("WARNING: Could not find torrent for `%s`" % release_name)
                        continue

                self.add_torrent_file("%s (%d)" % (title, year), torrent_url)
                self.mark_processed(entry_stats)

    def check_rotten_tomatoes(self, name):
        # query RT and get score for film
        qurl = lambda q: self.rotten_tomatoes_search_url.replace('%%query%%', urllib.parse.quote_plus(q))

        print("Downloading: %s" % qurl(name))
        f_rt = urllib.request.urlopen(qurl(name)).read().decode('utf-8', 'ignore')

        scores = {
            "critics": -1,
            "users": -1
        }

        critics_regexp = re.compile(r".*?(\d+)\% of critics liked it", re.S | re.I)
        m = critics_regexp.match(f_rt)
        if m:
            try:
                scores['critics'] = int(m.group(1))
            except ValueError:
                print("Could not parse as integer: %s" % m.group(1))

        users_regexp = re.compile(r".*?(\d+)\% of users liked it", re.S | re.I)
        m = users_regexp.match(f_rt)
        if m:
            try:
                scores['users'] = int(m.group(1))
            except ValueError:
                print("Could not parse as integer: %s" % m.group(1))

        return scores

    def check_torrent(self, name):
        # look up `name` on torrent site
        qurl = lambda q: self.torrent_search_url.replace('%%query%%', urllib.parse.quote_plus(q))
        print("Loading: %s" % qurl(name))
        parsed = feedparser.parse(qurl(name))
        if parsed and parsed.entries:
            if parsed.entries[0]['torrent_magneturi']:
                return parsed.entries[0]['torrent_magneturi']
            for link in parsed.entries[0].links:
                if "bittorrent" in link.type.lower():
                    return link.href
        return None

    def add_torrent_file(self, title, url):
        print("Adding torrent: \"%s\" -> %s" % (title, url))
        self.add_to_rss(title, url)

    def mark_processed(self, entry_stats):
        # mark as processed
        self.cached_entries.append(entry_stats)
        with open(self.cache_file, 'wb') as fh_cache_file:
            pickle.dump(self.cached_entries, fh_cache_file)

    def add_to_rss(self, title, link):
        try:
            fh = open(self.rss_file_output, "r")
        except FileNotFoundError:
            fh = open(self.rss_file_output, "w")
            # file is blank
            self.build_new_rss(fh)
            fh.close()
            fh = open(self.rss_file_output, "r")

        content = []
        for line in fh:
            content.append(line)
            if "<!-- ITEMS BEGIN -->" in line:
                # append new lines here
                break

        # Mon, 14 Mar 2016 12:09:57 +1300
        date = strftime("%a, %d %b %Y %H:%M:%S %z")

        writeline = """\
        <item>
            <title><![CDATA[{0}]]></title>
            <link><![CDATA[{1}]]></link>
            <pubDate>{2}</pubDate>
        </item>
"""

        content.append(writeline.format(title, link, date))

        for line in fh:
            content.append(line)

        fh.close()
        with open(self.rss_file_output, "w") as fh:
            fh.writelines(content)

    def build_new_rss(self, fh):
        fh.writelines("""\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
	xmlns:content="http://purl.org/rss/1.0/modules/content/"
	xmlns:wfw="http://wellformedweb.org/CommentAPI/"
	xmlns:dc="http://purl.org/dc/elements/1.1/"
	xmlns:atom="http://www.w3.org/2005/Atom"
	xmlns:sy="http://purl.org/rss/1.0/modules/syndication/"
	xmlns:slash="http://purl.org/rss/1.0/modules/slash/"
	>
    <channel>
        <title>Local Torrents Feed</title>
        <description>Built from Python</description>
        <!-- ITEMS BEGIN -->
    </channel>
</rss>
""")

if __name__ == "__main__":
    downloader = RSSFilmDownloader(cache_file, rss_file_output, torrent_search_url, rotten_tomatoes_search_url)
    downloader.parse_feed(rss_feeds, allowed_qualities, minimum_year, banned_genres, min_rt_score)
