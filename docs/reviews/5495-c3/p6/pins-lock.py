"""dependency-pins.lock du kit L2 = runtime-pins.txt + runtime-build-pins.txt à M, une ligne par
épingle (continuations jointes), triées par nom de distribution (ordre stable). Même règle que le
kit 5c1 : vérifiée en reproduisant à l'identique le lock de 5c1 depuis ses épingles."""
import re, subprocess, sys
src, rev = sys.argv[1], sys.argv[2]
text = "".join(subprocess.run(["git", "-C", src, "show", f"{rev}:deploy/mise-a-jour/{f}"], check=True,
               capture_output=True, text=True).stdout + "\n" for f in ("runtime-pins.txt", "runtime-build-pins.txt"))
text = re.sub(r"\\\n\s*", " ", text)
pins = [re.sub(r"\s+", " ", l.strip()) for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
pins.sort(key=lambda l: re.split(r"[\[=<> ]", l)[0].lower())
print("\n".join(pins))
