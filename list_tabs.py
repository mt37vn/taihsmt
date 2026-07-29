import json, sys

tabs = json.load(sys.stdin)
for t in tabs:
    print("  [{}] {} | {}".format(
        t.get('id','')[:40], t.get('title','')[:60], t.get('url','')[:80]
    ))
print("Total: {} tabs".format(len(tabs)))
