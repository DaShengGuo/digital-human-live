import re
data = open(r'C:\Users\Public\Desktop\直播伴侣.lnk', 'rb').read()
tokens = re.split(rb'[^ -~]+', data)
for t in tokens:
    s = t.decode('ascii', errors='ignore')
    if '.exe' in s.lower() or ('\\' in s and len(s) > 6):
        print(s)
