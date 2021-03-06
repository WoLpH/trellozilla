#!/usr/bin/env python

import re
import sys
import pyfscache
import HTMLParser
from datetime import datetime

try:
    from collections import OrderedDict
except:
    from ordereddict import OrderedDict

import utils
import trello
import settings

# Enabling a tiny bit of cache so Trello doesn't block us
cache = pyfscache.FSCache('cache', minutes=2)
long_cache = pyfscache.FSCache('cache', days=1)

# HTML parser for unescaping of html stuff
parser = HTMLParser.HTMLParser()

session = utils.get_session()


client = trello.TrelloClient(
    api_key=settings.API_KEY,
    api_secret=settings.API_SECRET,
    token=settings.TOKEN,
    token_secret=settings.TOKEN_SECRET,
)


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


def get_partial(card):
    match = re.search(r'\s*- (deel \d{1}[^\[(]*)', card.name)
    if match:
        return match.group(1)


def get_estimate(card):
    match = re.search(r'\((\d{1,2})\)', card.name)
    if match and 1 <= int(match.group(1)) < 40:
        return int(match.group(1))


def get_time_spent(card):
    match = re.search(r'\[(\d{1,2})\]', card.name)
    if match and 1 <= int(match.group(1)) < 40:
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

        new_description = settings.DESCRIPTION_PATTERN % dict(
            bug_id=card.bug_id,
            url=settings.BUGZILLA_BUG_URL % dict(id=card.bug_id),
            description=description.strip(),
        )
        return new_description.strip()


def get_name(card):
    text = get_bugzilla_page(card.bug_id)
    match = re.search(r'<title>Bug %s - ([^<]+)</title>' % card.bug_id, text)

    if match:
        name = parser.unescape(match.group(1)).decode('utf-8', 'replace')
        name_parts = [u'#%d' % card.bug_id]
        if card.estimate:
            name_parts.append(u'(%d)' % card.estimate)

        if card.time_spent:
            name_parts.append(u'[%d]' % card.time_spent)

        name_parts.append(u'-')
        name_parts.append(name)

        if card.partial:
            name_parts.append(u'-')
            name_parts.append(card.partial)

        return u' '.join(name_parts)


def update_priority(card):
    text = get_bugzilla_page(card.bug_id)
    match = re.search(r'<option value="(P\d+)" selected>', text)
    if match:
        priority = match.group(1)

        for label in card.labels:
            if label.name == priority:
                # Already has this priority, skip...
                return
            elif settings.PRIORITY_RE.match(label.name):
                print 'Should remove %r from %r' % (label, card)

        print 'Adding priority %s to %s' % (priority, card)
        priority_label = get_priority_label(card.board, priority)
        if priority_label:
            card.add_label(priority_label)
        else:
            print 'Label for priority %s does not exist' % priority


def get_bugzilla_comments(card, from_trello=False):
    text = get_bugzilla_page(card.bug_id)

    comments_re = re.compile('''
        <pre\s+id="comment_text_(?P<comment_id>\d+)">\s*
            (?P<comment>.+?)\s*
        </pre>
    ''', re.VERBOSE | re.DOTALL | re.UNICODE)

    submitter_re = re.compile('''
        <a\s+href="mailto:(?P<email>[^"]+)">(?P<name>.+?)\s+&lt;.+?
        Opened:\s+(?P<timestamp>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2})
    ''', re.VERBOSE | re.DOTALL | re.UNICODE)

    authors_re = re.compile('''
        <span\s+class="bz_comment">.+?
            <a\s+href="mailto:(?P<email>[^"]+)">(?P<name>.+?)</a>\s+
                (?P<timestamp>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2})
    ''', re.VERBOSE | re.DOTALL | re.UNICODE)

    authors = [submitter_re.search(text).groupdict()]
    authors += [m.groupdict() for m in authors_re.finditer(text)]

    bugzilla_url = settings.BUGZILLA_BUG_URL % dict(id=card.bug_id)

    comments = OrderedDict()
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
        if from_trello:
            if not comment['comment'].startswith('From Trello: '):
                continue
        else:
            if comment['comment'].startswith('From Trello: '):
                continue

        for k, v in comment.items():
            if isinstance(v, basestring):
                v = parser.unescape(v).strip()
                v = strip_tags(v)
                comment[k] = v

        comments[comment['url']] = comment

    return comments


def add_comments(card):
    comments = get_bugzilla_comments(card)

    # Remove all comments that have been added already
    for comment in get_comments(card):
        for k in comments.keys():
            if k in comment['data']['text']:
                del comments[k]

    for url, comment in sorted(comments.iteritems()):
        if 'from Trello:' in comment['comment']:
            continue

        formatted_comment = settings.COMMENT_PATTERN % comment
        print 'Adding comment', formatted_comment.split('\n')[0]
        card.comment(formatted_comment)


def update_card(card):
    if card.closed:
        print 'Deleting archived card %r' % card
        card.delete()
        return

    card.bug_id = get_bug_id(card)
    card.partial = get_partial(card)
    card.estimate = get_estimate(card)
    card.time_spent = get_time_spent(card)
    if card.bug_id:
        print 'Bug ID: %d' % card.bug_id, card.name

        new_name = get_name(card)
        if card.name.decode('utf-8', 'replace') != new_name:
            print 'Setting name from:\n%s\nTo:\n%s' % (
                card.name.decode('utf-8', 'replace'), new_name)
            card.set_name(new_name)

        new_description = get_description(card)
        if card.description != new_description:
            print 'setting description from: %r\nTo: %r' % (
                card.description,
                new_description,
            )
            card.set_description(new_description)

        add_comments(card)
        update_priority(card)
    else:
        print 'Unable to match: %s' % card.name


def get_bugzilla_page(bug_id):
    key = 'bugzilla_page', bug_id
    if key not in cache:
        url = settings.BUGZILLA_BUG_URL % dict(id=bug_id)
        request = session.get(url)
        cache[key] = request.text
    return cache[key]


@cache
def get_comments(card):
    return card.get_comments()


@long_cache
def get_labels(board):
    return board.get_labels()


def get_priority_label(board, priority):
    for label in get_labels(board):
        if settings.PRIORITY_RE.match(label.name) and label.name == priority:
            return label


#@long_cache
def list_boards():
    return client.get_organization('4tu').all_boards()


def get_board(board_id):
    for board in list_boards():
        if board.id == board_id:
            return board


@cache
def list_bugzilla_bugs(board):
    bug_ids = []
    if board.id not in settings.BUGZILLA_BOARD_MAPPING:
        return bug_ids

    url = settings.BUGZILLA_LIST_URL % dict(
        product=settings.BUGZILLA_BOARD_MAPPING[board.id])
    request = session.get(url)
    for match in settings.BUG_ID_PATTERN.finditer(request.text):
        bug_ids.append(int(match.group('bug_id')))

    return bug_ids


def get_cards(board):
    return list(board.all_cards())

if __name__ == '__main__':
    bugzilla_ids_per_project = {}
    all_trello_ids = set()

    if not sys.argv[1:]:
        for board in list_boards():
            print 'pre-processing %s: %r' % (board.id, board)
            # bugzilla_ids = set(list_bugzilla_bugs(board))
            # bugzilla_ids_per_project[board] = bugzilla_ids

            # for card in get_cards(board):
            #     all_trello_ids.add(get_bug_id(card))

        for board, ids in bugzilla_ids_per_project.iteritems():
            ids -= all_trello_ids
            if not ids:
                continue

            for list_ in board.all_lists():
                if list_.name.lower() == 'new':
                    break
            else:
                list_ = board.add_list('NEW')

            print 'Adding %d bugs to %s backlog' % (len(ids), board)
            for bug_id in ids:
                print 'Added card for bug %d' % bug_id
                list_.add_card(str(bug_id))

    active_bugs = dict()
    for card in get_cards(get_board(settings.ACTIVE_BOARD)):
        bug_id = get_bug_id(card)
        if bug_id:
            active_bugs[bug_id] = card

    archived_bugs = dict()
    for card in get_cards(get_board(settings.ARCHIVE_BOARD)):
        archived_bugs[get_bug_id(card)] = card

    for board in list_boards():
        print 'Processing %r' % board

        if sys.argv[1:]:
            cards = get_cards(board)
            selected = []
            for card in cards:
                for arg in sys.argv[1:]:
                    if arg not in card.name:
                        continue

                    selected.append(card)
        else:
            selected = []
            for card in get_cards(board):
                bug_id = get_bug_id(card)
                partial = 'deel ' in card.name
                if board.id == settings.ACTIVE_BOARD:
                    selected.append(card)
                elif bug_id and bug_id in active_bugs and not partial:
                    print 'Deleting duplicate card %r (in active)' % card
                    print 'Original: %r' % active_bugs[bug_id]
                    card.delete()
                elif bug_id and bug_id in archived_bugs and not partial \
                        and board.id != settings.ARCHIVE_BOARD:
                    print 'Deleting duplicate card %r (in archive)' % card
                    card.delete()
                else:
                    selected.append(card)

        # selected = sorted(selected, key=lambda c: c.name)

        # print 'Got %d cards' % len(selected)

        # if len(selected) == 1:
        #     update_card(*selected)
        # else:
        #     # pool = multiprocessing.Pool(4)
        #     # pool.map(update_card, selected)
        #     map(update_card, selected)

    utils.save_session(session)

