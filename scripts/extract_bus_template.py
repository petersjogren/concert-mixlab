"""One-off: extract a Reason-created output-bus Mix Channel DEVI ("Bus 1") into reason_templates/mix_channel_bus.iff."""
import sys
from pathlib import Path
sys.path.insert(0, "/Users/petersjogren/GitHubProjects/reason_spec")
import reasonspec
from reasonspec.chunks import serialize_list

src = sys.argv[1]
c, doc = reasonspec.load(src)
d = next(d for d in doc.rs10.find(b"DEVL").children if b"Bus 1" in d.find(b"DESC").data)
print([(getattr(n, "id", None), getattr(n, "kind", None)) for n in d.children])
out = Path(__file__).resolve().parent.parent / "reason_templates" / "mix_channel_bus.iff"
out.write_bytes(serialize_list([d]))
print(out, out.stat().st_size)
