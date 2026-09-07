#Quick check: read a few rows from prices.db and print them.

import sqlite3

conn = sqlite3.connect("prices.db") #This should be the name of the database
                                    #file created by scraper.py
cur = conn.execute(
    "SELECT name, brand, price, unit_price, promo_message FROM price_history LIMIT 5"
)

for row in cur.fetchall():
    name, brand, price, unit_price, promo = row
    print(f"{name} ({brand}) - {price} EUR - {unit_price} - {promo or 'no promo'}")

conn.close()