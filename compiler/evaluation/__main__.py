"""python3 -m evaluation  -- run the whole R4.6.5 characterization."""
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import runner, report, plots

rows, csv_path = runner.run()
text = report.render(rows)
print(text)
extra = report.write_summaries(rows)
imgs = plots.emit(rows)
open(os.path.join(_HERE, 'results', 'report.txt'), 'w').write(text)
print("  artifacts:", ", ".join(os.path.basename(p) for p in [csv_path] + extra + imgs))
