#!/usr/bin/python

import argparse, binascii, errno, hashlib, os, os.path, stat, string, sqlite3
import sys, time

import libdds

class Store(object):
    def __init__(self, dirname, auto_create=False):
        self.dirname = dirname

        if not os.path.exists(self.dirname):
            if auto_create:
                self._create()
            else:
                raise RuntimeError("archive %s not found" % self.dirname)
        elif not os.path.isdir(self.dirname):
            raise RuntimeError("%s exists but is not a ddar directory" %
                               self.dirname)

        try:
            format_name = self._read_small_file(self._format_filename('name'))
        except:
            raise RuntimeError('%s is not a ddar directory' %
                               self.dirname)
        else:
            if format_name.lstrip().rstrip() != 'ddar':
                raise RuntimeError('%s is not a ddar directory' %
                                   self.dirname)

        version = self._read_small_file(self._format_filename('version'))
        version = version.lstrip().rstrip()
        if version != '1':
            raise RuntimeError('%s uses ddar archive version %s but only' +
                               'version 1 is supported' % self.dirname)

        self.db = sqlite3.connect(os.path.join(self.dirname, 'db'))
        self.db.text_factory = str
        self.db.execute('PRAGMA foreign_keys = ON')

    @staticmethod
    def _read_small_file(name, size_limit=1024):
        with open(name, 'r') as f:
            data = f.read(size_limit)
        return data

    @staticmethod
    def _write_small_file(name, data):
        with open(name, 'w') as f:
            f.write(data)

    def _create(self):
        os.mkdir(self.dirname)
        os.mkdir(os.path.join(self.dirname, 'format'))
        os.mkdir(os.path.join(self.dirname, 'objects'))

        self._write_small_file(self._format_filename('name'), "ddar\n")
        self._write_small_file(self._format_filename('version'), "1\n")

        db = sqlite3.connect(os.path.join(self.dirname, 'db'))
        c = db.cursor()
        c.execute('''
CREATE TABLE archive (id INTEGER PRIMARY KEY,
                      name TEXT UNIQUE NOT NULL,
                      length INTEGER,
                      hash BLOB)''')
        c.execute('''
CREATE TABLE chunk (archive_id INTEGER NOT NULL,
                    hash BLOB NOT NULL,
                    offset INTEGER NOT NULL,
                    length INTEGER NOT NULL,
                    UNIQUE (archive_id, offset),
                    FOREIGN KEY (archive_id) REFERENCES archive(id))''')
        # No need for a (tag,offset) index as we have a UNIQUE
        # constraint on it. This causes sqlite to generate an index and
        # the documentatation states that there is a performance degradation
        # by having an extra index instead of relying on this fact.
        c.execute('CREATE INDEX chunk_hash_idx ON chunk(hash)')
        c.close()
        db.commit()
        db.close()

    def _object_filename(self, h):
        return os.path.join(self.dirname, 'objects', binascii.hexlify(h))

    def _format_filename(self, n):
        return os.path.join(self.dirname, 'format', n)

    def _store_chunk(self, archive_id, cursor, data, offset, length):
        '''Store the chunk in the database and the object store if necessary,
        but do not commit. The database insert is done last, so that if
        interrupted the database is never wrong. At worst a dangling object
        will be left in the object store.'''

        h = hashlib.sha256(data).digest()
        h_blob = buffer(h) # buffer to make sqlite use a BLOB
        cursor.execute('SELECT 1 FROM chunk WHERE hash=?', (h_blob,))
        if not cursor.fetchone():
            with open(self._object_filename(h), 'wb') as f:
                f.write(data)
        cursor.execute('INSERT INTO chunk ' +
                       '(archive_id, hash, offset, length) ' +
                       'VALUES (?, ?, ?, ?)',
                       (archive_id, h_blob, offset, length))

    def store(self, tag, f=sys.stdin, aio=False):
        cursor = self.db.cursor()

        cursor.execute('SELECT 1 FROM archive WHERE name=? LIMIT 1', (tag,))
        if cursor.fetchone():
            raise RuntimeError("archive %s already exists" % tag)

        cursor.execute('INSERT INTO archive (name) VALUES (?)', (tag,))
        archive_id = cursor.lastrowid
        cursor.execute('DELETE FROM chunk WHERE archive_id=?', (archive_id,))

        d = libdds.DDS()
        d.set_file(f)
        if aio:
            d.set_aio()
        d.begin()

        try:
            offset = 0
            h = hashlib.sha256()
            for data in d.chunks():
                length = len(data)
                self._store_chunk(archive_id, cursor, data, offset, length)
                h.update(data)
                offset += length
        except:
            raise
        else:
            h_blob = buffer(h.digest())
            cursor.execute('UPDATE archive SET hash=?, length=? WHERE ' +
                           'id=?', (h_blob, offset, archive_id))
        finally:
            cursor.close()
            self.db.commit()

    def load(self, tag, f=sys.stdout):
        cursor = self.db.cursor()
        cursor.execute('SELECT id, hash FROM archive WHERE name=?', (tag,))
        row = cursor.fetchone()
        if not row:
            raise RuntimeError('member %s not found in archive' % tag)

        archive_id, h = row
        expected_h = str(h)
        cursor.execute('SELECT hash, offset, length ' +
                       'FROM chunk WHERE archive_id=? ' +
                       'ORDER BY offset', (archive_id,))

        h2 = hashlib.sha256()
        next_offset = 0
        row = cursor.fetchone()
        while row:
            h, offset, length = row
            assert(offset == next_offset)
            with open(self._object_filename(h), 'rb') as g:
                data = g.read()
            assert(len(data) == length)
            h2.update(data)
            f.write(data)

            next_offset = offset + length
            row = cursor.fetchone()

        if expected_h != h2.digest():
            raise RuntimeError('Extracted archive failed hash check')

    def delete(self, tag):
        cursor = self.db.cursor()
        cursor2 = self.db.cursor()
        cursor.execute('SELECT id FROM archive WHERE name=?', (tag,))
        row = cursor.fetchone()
        if not row:
            raise RuntimeError('member %s not found in archive' % tag)
        archive_id = row[0]
        cursor.execute('SELECT hash FROM chunk WHERE archive_id=?',
                       (archive_id,))
        row = cursor.fetchone()
        while row:
            h = row[0]
            cursor2.execute('SELECT 1 FROM chunk WHERE hash=? AND ' +
                            'archive_id != ? LIMIT 1', (h, archive_id))
            if not cursor2.fetchone():
                try:
                    os.unlink(self._object_filename(h))
                except OSError, e:
                    # ignore ENOENT to make delete idempotent on a SIGINT
                    if e.errno != errno.ENOENT:
                        raise
            row = cursor.fetchone()
        cursor.execute('DELETE FROM chunk WHERE archive_id=?', (archive_id,))
        cursor.execute('DELETE FROM archive WHERE id=?', (archive_id,))
        cursor.close()
        self.db.commit()

    def list_tags(self):
        cursor = self.db.cursor()
        cursor.execute('SELECT name FROM archive')
        tag = cursor.fetchone()
        while tag:
            yield tag[0]
            tag = cursor.fetchone()

    def get_last_tag(self):
        cursor = self.db.cursor()
        cursor.execute('SELECT name FROM archive ORDER BY rowid DESC LIMIT 1')
        row = cursor.fetchone()
        return row[0] if row else None

    def suggest_tag(self):
        cursor = self.db.cursor()
        base = time.strftime('%Y-%m-%d')
        cursor.execute('SELECT 1 FROM archive WHERE name=?', (base,))
        if not cursor.fetchone():
            return base
        root_like = base + '-%'
        cursor.execute('SELECT COUNT(*) FROM archive WHERE name LIKE ?',
                       (root_like,))
        row = cursor.fetchone()
        if not row or not row[0]:
            return '%s-2' % base

        suffix = row[0] + 1

        cursor.execute('SELECT 1 FROM archive WHERE name=?',
                       ('%s-%s' % (base, suffix),))
        row = cursor.fetchone()
        while row:
            suffix += 1
            cursor.execute('SELECT 1 FROM archive WHERE name=?',
                           ('%s-%s' % (base, suffix),))
            row = cursor.fetchone()

        return '%s-%s' % (base, suffix)

    hexdigits = set(string.hexdigits) - set(string.uppercase)
    sha256_length = len(hashlib.sha256().hexdigest())

    @classmethod
    def _valid_hex_hash(cls, h):
        return (len(h) == cls.sha256_length and
            all((d in cls.hexdigits for d in h)))

    def _fsck_fs(self):
        cursor = self.db.cursor()
        for h in os.listdir(os.path.join(self.dirname, 'objects')):
            if not self._valid_hex_hash(h):
                continue
            cursor.execute('SELECT 1 FROM chunk WHERE hash=? LIMIT 1',
                           (buffer(binascii.unhexlify(h)),))
            if not cursor.fetchone():
                print 'Unknown object %s' % h

    def _fsck_db_to_fs_chunk(self):
        cursor = self.db.cursor()

        # Check each hash is correct
        cursor.execute('SELECT DISTINCT hash, length FROM chunk')

        row = cursor.fetchone()
        while row:
            h, length = row
            h = str(h) # sqlite3 returns a buffer for a BLOB; we want an str;
                       # otherwise comparisons never match
            try:
                with open(self._object_filename(h), 'rb') as f:
                    data = f.read(length+1)
            except IOError:
                print "Could not read chunk %s" % binascii.hexlify(h)
            else:
                if len(data) != length:
                    print "Chunk %s wrong size" % binascii.hexlify(h)
                elif hashlib.sha256(data).digest() != h:
                    print "Chunk %s corrupt" % binascii.hexlify(h)
            
            row = cursor.fetchone()

    def _fsck_db_to_fs_archive(self):
        cursor = self.db.cursor()
        cursor2 = self.db.cursor()
        # Check each archive hash is correct
        cursor.execute('SELECT id, name, hash FROM archive')
        row = cursor.fetchone()
        while row:
            archive_id, tag, h = row
            h = str(h)

            cursor2.execute('SELECT hash FROM chunk WHERE archive_id=? ' +
                            'ORDER BY offset', (archive_id,))

            h2 = hashlib.sha256()
            row2 = cursor2.fetchone()
            while row2:
                chunk_hash = row2[0]
                try:
                    with open(self._object_filename(chunk_hash), 'rb') as f:
                        data = f.read()
                except IOError:
                    print ("Could not read chunk %s from %s" %
                            (binascii.hexlify(chunk_hash), tag))
                else:
                    h2.update(data)

                row2 = cursor2.fetchone()

            if h != h2.digest():
                print '%s: hash mismatch' % tag

            row = cursor.fetchone()

    def _fsck_db(self):
        cursor = self.db.cursor()
        cursor2 = self.db.cursor()

        # Check through each archive in the chunk table
        cursor.execute('SELECT archive_id, offset, length FROM chunk ' +
                       'ORDER BY archive_id, offset')
        current_archive_id = None
        row = cursor.fetchone()
        while row:
            archive_id, offset, length = row
            if archive_id != current_archive_id:
                current_archive_id = archive_id
                current_offset = 0
                tag = cursor2.execute('SELECT name FROM archive WHERE id=?',
                                      (archive_id,)).fetchone()[0]
            if offset != current_offset:
                if offset < current_offset:
                    print ("Chunk at offset %d for %s has an overlap" %
                        (offset, tag))
                else:
                    assert(offset > current_offset)
                    print ("Hole in %s found between %d and %d" %
                        (tag, current_offset, offset))
                current_offset = offset
            current_offset += length

            row = cursor.fetchone()

        # Check all lengths in archive match chunk totals
        cursor.execute('''
SELECT archive.name

FROM
    archive,

    (SELECT archive_id, sum(length) AS length
     FROM chunk GROUP BY archive_id) AS chunk_lengths

WHERE
      archive.id = chunk_lengths.archive_id
      AND archive.length != chunk_lengths.length''')
        
        row = cursor.fetchone()
        while row:
            print 'Length mismatch in %s' % row[0]
            row = cursor.fetchone()

    def fsck(self):
        self._fsck_db()
        self._fsck_fs()
        self._fsck_db_to_fs_chunk()
        self._fsck_db_to_fs_archive()

class OptionError(RuntimeError):
    def __init__(self, m):
        self.message = m

class OptionHelpRequest(Exception): pass

def parse_args(args):
    # The argument parser options are modelled after GNU ar, tar, gzip and
    # tarsnap. The aim is to not confuse or annoy anyone already familiar with
    # those tools by behaving as one would expect. Unfortunately I can't seem
    # to find a way to make this happen using argparse, optparse or getopt, so
    # doing it by hand it is.

    result = {}
    pos_args = []
    pos_arg_names = [ 'member' ]

    bool_options = set('ctxd') | set([ 'fsck', 'force-stdout' ])
    arg_options = set([ 'f', 'L' ])
    command_options = set([ 'c', 't', 'x', 'd', 'fsck' ])

    all_options = bool_options | arg_options | command_options
    single_letter_options = set([x for x in all_options if len(x) == 1])
    long_options = set([x for x in all_options if len(x) > 1])

    def set_result(k, v):
        if k in command_options and any((k in result for k in command_options)):
            raise OptionError('only one command option may be specified')
        result[k] = v

    def parse_long_option(arg_iter, arg):
        arg = arg.lstrip('-')

        if arg == 'help':
            raise OptionHelpRequest()

        if arg in arg_options:
            set_result(arg, arg_iter.next())
        elif arg in bool_options:
            set_result(arg, True)
        else:
            raise OptionError('unknown option: %s' % arg)

    def parse_option_cluster(arg_iter, arg):
        arg = arg.lstrip('-')
        while arg:
            if arg[0] in single_letter_options:
                if arg[0] in arg_options:
                    if len(arg) == 1:
                        set_result(arg[0], arg_iter.next())
                    else:
                        set_result(arg[0], arg[1:])
                    return
                elif arg[0] in bool_options:
                    set_result(arg[0], True)
                    arg = arg[1:]
                    continue
                else:
                    raise NotImplementedError()
            elif arg[0] == 'h':
                raise OptionHelpRequest()
            else:
                raise OptionError('unknown option: %s' % arg[0])

    arg_iter = iter(args)
    first = True
    while True:
        try:
            arg = arg_iter.next()
        except StopIteration:
            break
        if first and not arg.startswith('--'):
            # First arg is always the command even without a '-', like ps
            # and tar do it, unless it is a long option
            parse_option_cluster(arg_iter, arg)
        elif arg == '--':
            # Remaining args are all positional
            pos_args.extend(arg_iter)
            break
        elif not arg:
            # An empty arg? I guess it's positional
            pos_args.append(arg)
        elif arg == '-':
            # This is a positional arg too
            pos_args.append(arg)
        elif arg.startswith('--'):
            parse_long_option(arg_iter, arg)
        elif arg.startswith('-'):
            parse_option_cluster(arg_iter, arg)
        else:
            pos_args.append(arg)
        first = False

    result.update(zip(pos_arg_names, pos_args))

    last_pos_arg_name = pos_arg_names[-1]
    try:
        result[last_pos_arg_name] = [ result[last_pos_arg_name] ]
    except KeyError:
        result[last_pos_arg_name] = []
    result[last_pos_arg_name].extend(pos_args[len(pos_arg_names):])

    for opt in list(all_options) + pos_arg_names:
        if opt not in result:
            result[opt] = None

    return result

def main_add_one(store, filename, tag):
    if filename == '-':
        store.store(tag, sys.stdin)
    else:
        with open(filename, 'rb') as f:
            store.store(tag, f, aio=True)

def main_add(store, members, tag=None):
    if not members:
        if not tag:
            tag = store.suggest_tag()
        main_add_one(store, '-', tag)
    elif len(members) == 1:
        if not tag:
            tag = members[0]
        main_add_one(store, members[0], tag)
    else:
        for member in members:
            main_add_one(store, member, member)

def main_extract(store, members):
    if not members:
        members = [ store.get_last_tag() ]
    for tag in members:
        store.load(tag, sys.stdout)

def main():
    try:
        args = parse_args(sys.argv[1:])
        if not any((args[k] for k in (list('cxtd') + ['fsck']))):
            raise OptionError('a command is required')
        if not args['f']:
            try:
                args['f'] = args['member'].pop(0)
            except IndexError:
                raise OptionError('an archive must be specified')
        if args['d'] and not args['member']:
            raise OptionError('no member specified')
        if args['L'] and not args['c']:
            raise OptionError('option -L not valid except in create mode')

        store = Store(args['f'], auto_create=args['c'])
        if args['c']:
            main_add(store, args['member'], args['L'])
        elif args['x']:
            if not args['force-stdout'] and os.isatty(sys.stdout.fileno()):
                raise OptionError('output is a terminal and --force-stdout not specified')
            main_extract(store, args['member'])
        elif args['d']:
            for member in args['member']:
                store.delete(member)
        elif args['t']:
            for tag in store.list_tags():
                print tag
        elif args['fsck']:
            store.fsck()

    except OptionHelpRequest:
        print '''ddar: store multiple files efficiently in a de-duplicated archive

Create or add to an archive:
    ddar [-]c [-f] archive [-L member-name] < file
    ddar [-]c [-f] archive [-L member-name] member
    ddar [-]c [-f] archive member [member...]

    If member-name is unspecified, ddar will use the supplied filename
    as the member name, or for stdin create a suitable name based on the
    current date.

Extract from an archive:
    ddar [-]x [options] [-f] archive > file  # extract the most recent member
    ddar [-]x [options] [-f] archive member-name > file

    Options:
        --force-stdout  Write to stdout even if stdout is a terminal

List members in an archive:
    ddar [-]t [-f] archive

Delete members from an archive:
    ddar [-]d [-f] archive member-name [member-name...]

Check an archive for integrity:
    ddar --fsck [-f] archive


Examples:
    Back up your home directory daily:
        tar c ~|gzip --rsyncable|ddar cf /mnt/external_disk/home_backup

    Restore your home directory after a disaster:
        ddar xf /mnt/external_disk/home_backup|tar xzC/
'''
        return
    except OptionError, e:
        print 'ddar: %s' % e.message
        print 'Try: ddar --help'
        return

if __name__ == '__main__':
    main()

# vim: set ts=8 sts=4 sw=4 ai et :
