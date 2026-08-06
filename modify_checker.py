import re

with open('telegram_checker/checker.py', 'r') as f:
    content = f.read()

# We will just write a new checker.py based on the old one, but updated.
# Since it's complex, let's create a backup first.
