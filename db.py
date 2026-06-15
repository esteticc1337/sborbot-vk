import os

import psycopg2

DATABASE_URL = os.environ.get('DATABASE_URL')
select_users_query = "SELECT * FROM users;"
select_day_query = "SELECT day_num FROM day;"
insert_user_query = "INSERT INTO users (id, songs_output_type) VALUES(%s, %s);"
update_day_query = "UPDATE day SET day_num = (%s) WHERE id = 1"
update_songs_query = "UPDATE users SET songs_output_type = (%s) WHERE id = (%s)"


def get_users():
    con = None
    try:
        con, cur = open_connection(con)
        cur.execute(select_users_query)
        return cur.fetchall()
    except (Exception, psycopg2.Error) as error:
        print("Error while fetching data from PostgreSQL", error)
    finally:
        if con:
            con.close()


def add_user(chat_id):
    con = None
    try:
        con, cur = open_connection(con)
        cur.execute(insert_user_query, (chat_id, 'file'))
        con.commit()
    except (Exception, psycopg2.Error) as error:
        print("Error while fetching data from PostgreSQL", error)
    finally:
        if con:
            con.close()


def fetch_current_day():
    con = None
    try:
        con, cur = open_connection(con)
        cur.execute(select_day_query)
        day = cur.fetchone()[0]
        return day
    except (Exception, psycopg2.Error) as error:
        print("Error while fetching data from PostgreSQL", error)
    finally:
        if con:
            con.close()


def update_current_day(day):
    con = None
    try:
        con, cur = open_connection(con)
        cur.execute(update_day_query, (day,))
        con.commit()
        return day
    except (Exception, psycopg2.Error) as error:
        print("Error while fetching data from PostgreSQL", error)
    finally:
        if con:
            con.close()


def update_songs_type(user_id, songs_type):
    con = None
    try:
        con, cur = open_connection(con)
        cur.execute(update_songs_query, (songs_type, user_id))
        con.commit()
    except (Exception, psycopg2.Error) as error:
        print("Error while fetching data from PostgreSQL", error)
    finally:
        if con:
            con.close()


def open_connection(con):
    con = psycopg2.connect(DATABASE_URL)
    cur = con.cursor()
    return con, cur
