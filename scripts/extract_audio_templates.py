"""One-off: extract the recording/comp templates (TRRF with one TREF, TNMF, SASF) from a Reason-saved song
that contains an imported audio clip, into reason_templates/audio_recording_templates.iff."""
import copy, sys
from pathlib import Path
sys.path.insert(0, "/Users/petersjogren/GitHubProjects/reason_spec")
import reasonspec
from reasonspec import sequencer as S, audio as AU
from reasonspec.chunks import Group, serialize_list, parse_list

HERE = Path(__file__).resolve().parent.parent / "reason_templates"
src = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "soundtest.reason"
c, doc = reasonspec.load(src)
t = next(t for t in S.tracks(doc) if AU.recordings(t))
trrf = copy.deepcopy(S._group(t.node, b"TRRF")); tnmf = copy.deepcopy(S._group(t.node, b"TNMF"))
trrl = S._group(trrf, b"TRRL")
trrl.children = [n for n in trrl.children if not (isinstance(n, Group) and n.kind == b"TREF")] + \
                [n for n in trrl.children if isinstance(n, Group) and n.kind == b"TREF"][:1]
sasf = copy.deepcopy(AU.all_audio_clips(t)[0].sasf)
b = serialize_list([trrf, tnmf, sasf])
(HERE / "audio_recording_templates.iff").write_bytes(b)
print(len(b), [(n.id, n.kind) for n in parse_list(b)])
