#!/usr/bin/env python

import re
import requests
import pyfscache
import HTMLParser
import collections
import multiprocessing

from datetime import datetime

# Enabling a tiny bit of cache so Trello doesn't block us
cache = pyfscache.FSCache('cache', minutes=2)

# HTML parser for unescaping of html stuff
parser = HTMLParser.HTMLParser()


BUGZILLA_URL = 'http://bugzilla.3xo.eu/show_bug.cgi?id=%(id)s'
DESCRIPTION_PATTERN = '''
[Bugzilla \#%(bug_id)s](%(url)s)
%(description)s
'''
COMMENT_PATTERN = '''
[By %(name)s at %(timestamp)s](%(url)s)
%(comment)s
'''.strip()

try:
    from client import client
except ImportError:
    print '''You need to create a trello client in `client.py`, it should look
    something like this:

    .. highlight:: python

        import trello

        client = trello.TrelloClient(
            api_key='your-key',
            api_secret='your-secret',
            token='your-oauth-token-key',
            token_secret='your-oauth-token-secret',
        )

    :api_key: API key generated at https://trello.com/1/appKey/generate
    :api_secret: the secret component of api_key
    :token_key: OAuth token generated by the user in
                trello.util.create_oauth_token
    :token_secret: the OAuth client secret for the given OAuth token

    You can get the actual input using the `trello_authenticate.py` script with
    the `api_key` and `api_secret` parameters.
    '''


class MLStripper(HTMLParser.HTMLParser):
    def __init__(self):
        self.reset()
        self.fed = []

    def handle_data(self, d):
        self.fed.append(d)

    def get_data(self):
        return ''.join(self.fed)


def strip_tags(html):
    s = MLStripper()
    s.feed(html)
    return s.get_data()


def get_bug_id(card):
    match = re.search(r'(\d{4})', card.name)
    if match and 1000 < int(match.group(1)) < 3000:
        return int(match.group(1))


def get_description(card):
    # Cleaning up old/broken link formats and whitespace
    description = card.description.strip()
    description = re.sub('\s+$', '', description)
    description = re.sub(r'\[(Bugzilla \\*#\d+|bugzilla)\].+\n', '',
                         description)
    description = re.sub(r'\(http://bugzilla[^)]+\)\n?', '', description)
    description = re.sub(r'\[Bugzilla [^\]]+\]\n?', '', description)
    description = description.strip()

    if(not re.search(r'\[(Bugzilla \#\d+)\]', description)
            or description != card.description.strip()):

        new_description = DESCRIPTION_PATTERN % dict(
            bug_id=card.bug_id,
            url=BUGZILLA_URL % dict(id=card.bug_id),
            description=description.strip(),
        )
        return new_description.strip()


def get_name(card):
    text = get_bugzilla_page(card.bug_id)
    match = re.search(r'<title>Bug %s - ([^<]+)</title>' % card.bug_id, text)

    if match:
        name = parser.unescape(match.group(1))
        return '#%(bug_id)s -  %(name)s' % dict(
            bug_id=card.bug_id,
            name=name,
        )


def add_comments(card):
    text = get_bugzilla_page(card.bug_id)

    comments_re = re.compile('''
        <pre\s+id="comment_text_(?P<comment_id>\d)">\s*
            (?P<comment>.+?)\s*
        </pre>
    ''', re.VERBOSE | re.DOTALL)

    submitter_re = re.compile('''
        <a\s+href="mailto:(?P<email>[^"]+)">(?P<name>.+?)\s+&lt;.+?
        Opened:\s+(?P<timestamp>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2})
    ''', re.VERBOSE | re.DOTALL)

    authors_re = re.compile('''
        <span\s+class="bz_comment">.+?
            <a\s+href="mailto:(?P<email>[^"]+)">(?P<name>.+?)</a>\s+
                (?P<timestamp>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2})
    ''', re.VERBOSE | re.DOTALL)

    authors = [submitter_re.search(text).groupdict()]
    authors += [m.groupdict() for m in authors_re.finditer(text)]

    bugzilla_url = BUGZILLA_URL % dict(id=card.bug_id)

    comments = collections.OrderedDict()
    for author, match in zip(authors, comments_re.finditer(text)):
        comment = dict(
            url=bugzilla_url + '#c' + match.group('comment_id'),
            comment_id=int(match.group('comment_id')),
            comment=match.group('comment'),
            timestamp=datetime.strptime(author['timestamp'],
                                        '%Y-%m-%d %H:%M'),
            email=author['email'],
            name=author['name'],
        )
        for k, v in comment.items():
            if isinstance(v, basestring):
                v = parser.unescape(v).strip()
                v = strip_tags(v)
                comment[k] = v

        comments[comment['url']] = comment

    # Remove all comments that have been added already
    for comment in get_comments(card):
        for k in comments.keys():
            if k in comment['data']['text']:
                del comments[k]

    for url, comment in comments.iteritems():
        formatted_comment = COMMENT_PATTERN % comment
        print 'Adding comment', formatted_comment.split('\n')[0]
        card.comment(formatted_comment)


def update_card(card):
    card.bug_id = get_bug_id(card)
    if card.bug_id:
        print 'Bug ID: %d' % card.bug_id

        new_name = get_name(card)
        if card.name != new_name:
            print 'Setting name from:\n%s\nTo:\n%s' % (card.name, new_name)
            card.set_name(new_name)

        new_description = get_description(card)
        if card.description != new_description:
            print 'setting description from: %r\nTo: %r' % (
                card.description,
                new_description,
            )
            card.set_description(new_description)

        add_comments(card)
    else:
        print 'Unable to match: %s' % card.name


@cache
def get_bugzilla_page(bug_id):
    url = BUGZILLA_URL % dict(id=bug_id)
    request = requests.get(url)
    return request.text


@cache
def get_comments(card):
    return card.get_comments()


@cache
def get_cards():
    board = client.get_board('54e476a396124a3eec92625a')
    return list(board.all_cards())

if __name__ == '__main__':
    pool = multiprocessing.Pool(8)
    pool.map(update_card, get_cards())

