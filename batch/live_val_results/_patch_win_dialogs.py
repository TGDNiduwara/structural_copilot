import io

p = "tools/win_dialogs.py"
s = open(p, encoding="utf-8-sig").read()

old1 = (
    "                matched = None\n"
    "                for key, spec in patterns.items():\n"
    "                    if key in low:\n"
    "                        matched = spec\n"
    "                        break"
)
new1 = (
    "                matched = None\n"
    "                matched_key = None\n"
    "                for key, spec in patterns.items():\n"
    "                    if key in low:\n"
    "                        matched = spec\n"
    "                        matched_key = key\n"
    "                        break"
)
assert s.count(old1) == 1, f"old1 count={s.count(old1)}"
s = s.replace(old1, new1)

old2 = '                    result["button"] = bt\n'
new2 = (
    '                    result["button"] = bt\n'
    '                    result["matched"] = matched_key\n'
)
assert s.count(old2) == 1, f"old2 count={s.count(old2)}"
s = s.replace(old2, new2)

open(p, "w", encoding="utf-8-sig", newline="").write(s)
print("patched ok")
