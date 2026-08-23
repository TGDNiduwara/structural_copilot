import io

p = "tools/win_dialogs.py"
s = open(p, encoding="utf-8-sig").read()

old2 = '                    result["button"] = bt\n'
new2 = (
    '                    result["button"] = bt\n'
    '                    result["matched"] = matched_key\n'
    '                    if matched_key == "instabilit":\n'
    '                        result["instability_seen"] = True\n'
)
assert s.count(old2) == 1, f"old2 count={s.count(old2)}"
s = s.replace(old2, new2)

open(p, "w", encoding="utf-8-sig", newline="").write(s)
print("patched instability_seen ok")
