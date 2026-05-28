from datetime import datetime, timedelta
from parse_xls_biff import parse_workbook

path = r"C:\Users\lee-y\Downloads\阿里国际站后台导出的原始回复明细表.xls"
sheet = parse_workbook(path)[0]
rows = sheet["preview"]

header = rows[5]
data = rows[6:]
idx = {name: header.index(name) for name in header if name}

def parse_dt(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")

records = []
for i, row in enumerate(data, 1):
    if not row or not row[idx["发送方姓名"]]:
        continue
    send_bj = parse_dt(row[idx["发送时间(PST)"]]) + timedelta(hours=16)
    hours = float(row[idx["回复时长(小时)"]] or 0)
    weekday = send_bj.isoweekday()
    non_work = int(weekday > 5 or send_bj.hour < 8 or send_bj.hour >= 18)
    fast = int(str(row[idx["是否极速回复（5分钟）"]]).strip().upper() in ("Y", "是", "1"))
    human = int(str(row[idx["极速回复率是否人工回复"]]).strip().upper() in ("Y", "是", "1"))
    non_work_unfast = int(non_work and not fast)
    fast_non_human = int(fast and not human)
    over_one = int(hours >= 1)
    severe = int(hours >= 6)
    risk = non_work_unfast * 2 + fast_non_human + over_one + severe * 3
    records.append((row[idx["及时回复负责人"]], fast, human, non_work, non_work_unfast, fast_non_human, over_one, severe, risk))

owners = {}
for owner, fast, human, non_work, non_work_unfast, fast_non_human, over_one, severe, risk in records:
    owners.setdefault(owner, [0] * 8)
    vals = owners[owner]
    for p, value in enumerate([1, fast, human, non_work, non_work_unfast, fast_non_human, over_one, severe]):
        vals[p] += value

print("records", len(records))
print("abnormal", sum(1 for r in records if r[-1] > 0))
for owner, v in owners.items():
    total, fast, human, non_work, non_work_unfast, fast_non_human, over_one, severe = v
    score = round((non_work_unfast / non_work if non_work else 0) * 40 + (fast_non_human / total) * 20 + (over_one / total) * 25 + (severe / total) * 15)
    print(owner, "total", total, "fast", fast, "human", human, "over1", over_one, "severe", severe, "score", score)
