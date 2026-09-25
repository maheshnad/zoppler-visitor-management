import os

import psycopg

source_url = os.environ['SOURCE_DATABASE_URL']
target_url = os.environ['TARGET_DATABASE_URL']

# Importing the application with the target URL creates and migrates its schema.
os.environ['DATABASE_URL'] = target_url
import server

with psycopg.connect(source_url) as source, psycopg.connect(target_url) as target:
    accounts = source.execute(
        '''SELECT username,email,password_hash,session_version
           FROM admins WHERE email IS NOT NULL ORDER BY id'''
    ).fetchall()
    if len(accounts) != 5:
        raise RuntimeError(f'Expected five management accounts; found {len(accounts)}')
    for username, email, password_hash, session_version in accounts:
        target.execute(
            '''INSERT INTO admins(username,email,password_hash,session_version)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (email) DO UPDATE SET
                 username=excluded.username,
                 password_hash=excluded.password_hash,
                 session_version=excluded.session_version''',
            (username, email, password_hash, session_version),
        )
    target.execute('DELETE FROM password_resets')

server.pool.close()
print('Cloud database initialized with five management accounts.')
