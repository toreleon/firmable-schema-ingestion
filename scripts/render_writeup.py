"""Render the Markdown report at readable A4 sizing and enforce its two-page limit.

Requires pandoc, WeasyPrint, and pypdf (pip install -r requirements-render.txt).
No network resources are used; SOURCE_APPENDIX.md remains a separate document.
"""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib,json,shutil,subprocess,tempfile
from pypdf import PdfReader
from weasyprint import HTML
import weasyprint

root=Path(__file__).resolve().parents[1]
pandoc=shutil.which('pandoc')
if not pandoc:
    raise SystemExit('Rendering requires pandoc on PATH')
pdf=root/'WRITEUP.pdf'
with tempfile.TemporaryDirectory(prefix='firmable-report-') as folder:
    folder=Path(folder);html=folder/'writeup.html'
    subprocess.run([pandoc,str(root/'WRITEUP.md'),'--standalone',
        '--metadata','pagetitle=Schema inference and entity identification',
        '--include-in-header',str(root/'scripts/writeup-style.html'),'-o',str(html)],check=True,capture_output=True)
    markup=html.read_text()
    for name in ['README.md','SOURCE_APPENDIX.md']:
        markup=markup.replace('href="'+name+'"','href="https://github.com/toreleon/firmable-schema-ingestion/blob/main/'+name+'"')
    HTML(string=markup,base_url=str(root)).write_pdf(str(pdf))
reader=PdfReader(pdf)
count=len(reader.pages)
proof={'rendered_at':datetime.now(ZoneInfo('Asia/Saigon')).isoformat(),'page_count':count,
    'paper':'A4','body_font_pt':10.5,'source_sha256':hashlib.sha256((root/'WRITEUP.md').read_bytes()).hexdigest(),
    'pdf_sha256':hashlib.sha256(pdf.read_bytes()).hexdigest(),
    'renderer':'WeasyPrint '+weasyprint.__version__,
    'markdown_renderer':subprocess.check_output([pandoc,'--version'],text=True).splitlines()[0],
    'source_appendix_in_separate_file':True}
(root/'outputs/writeup_render_validation.json').write_text(json.dumps(proof,indent=2)+'\n')
print(json.dumps(proof,indent=2))
if count!=2:
    raise SystemExit(f'Expected exactly two rendered pages; got {count}')
