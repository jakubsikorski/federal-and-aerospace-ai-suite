"""Exercise stock Chat Q&A document and retrieval-enabled general-chat APIs."""

import argparse
import io
import json
import os
from pathlib import Path
import re
from urllib.request import Request, urlopen
import uuid
from zipfile import ZipFile
from xml.sax.saxutils import escape


def api(base, path, body=None, content_type="application/json"):
    request = Request(base + path, data=body,
                      headers={"Content-Type": content_type} if body is not None else {})
    with urlopen(request, timeout=300) as response:
        return json.load(response)


def chat(base, question):
    result = api(base, "/chat", json.dumps({"input": question, "stream": False}).encode())
    if result.get("status") != "Success" or not isinstance(result.get("metadata"), str):
        raise RuntimeError(f"Unexpected /chat response: {result}")
    raw = result["metadata"].strip()
    # The stock HuggingFacePipeline echoes the full formatted prompt before the
    # completion (LangChain default return_full_text behavior). The pinned
    # neural-chat-7b-v3-3 prompt template (config/chatqna/config.yaml) places a
    # single literal "Answer:" marker immediately before generation and nowhere
    # else; splitting on its last occurrence isolates the model's actual
    # generated continuation from the echoed system/context/question text.
    marker = "Answer:"
    if marker not in raw:
        raise RuntimeError(f"Expected echoed prompt marker {marker!r} in /chat response: {raw!r}")
    return raw.rsplit(marker, 1)[1].strip()


def documents(base):
    result = api(base, "/documents")
    return result["metadata"]["documents"]


def pdf(text):
    lines = ["BT /F1 11 Tf 40 760 Td"]
    for index, line in enumerate(text.splitlines()):
        if index:
            lines.append("0 -18 Td")
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        lines.append(f"({safe}) Tj")
    stream = ("\n".join(lines) + "\nET\n").encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


def docx(text):
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>')
        archive.writestr("_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
        archive.writestr("word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            + "".join(f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>" for line in text.splitlines())
            + '</w:body></w:document>')
    return output.getvalue()


def ingest(base, output):
    run_id = uuid.uuid4().hex[:12]
    state = {"run_id": run_id, "location": f"ORCHID-{uuid.uuid4().hex[:8]}",
             "equipment": f"KIT-{uuid.uuid4().hex[:8]}"}
    texts = {
        "mission-plan.pdf": (
            f"Synthetic handheld mission {run_id}\n"
            "Objective: inspect the laboratory camera mount.\n"
            f"Inspection location: {state['location']}.\n"
            "Deadline: 14:35. Unit: Cedar team.\n"
            "Restriction: keep the emergency exit clear.\n"
            "Dependency: receive the facility access key first.\n"
            "Known risk: wet floor near the entrance.\n"
            "No individual people are named in this plan."
        ),
        "equipment.docx": (
            f"Equipment supplement for synthetic handheld mission {run_id}\n"
            f"Required equipment identifier: {state['equipment']}.\n"
            "Resource: one amber inspection case."
        ),
    }
    names = []
    for filename, text in texts.items():
        content = pdf(text) if filename.endswith(".pdf") else docx(text)
        (output / filename).write_bytes(content)
        upload_name = f"handheld-{run_id}-{filename}"
        boundary = uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{upload_name}"'
            '\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        result = api(base, "/documents", body, f"multipart/form-data; boundary={boundary}")
        (output / f"{filename}.response.json").write_text(json.dumps(result, indent=2))
        if result.get("status") != "Success":
            raise RuntimeError(f"Ingestion failed: {result}")
        names.append(upload_name)
    state["documents"] = names
    listed = documents(base)
    if not all(name in listed for name in names):
        raise RuntimeError(f"Ingested documents missing from /documents: {listed}")
    (output / "state.json").write_text(json.dumps(state, indent=2))
    print("PASS: synthetic PDF and DOCX ingested through stock /documents")


def rag(base, output):
    state = json.loads((output / "state.json").read_text())
    answers = {}
    for field, label in (("location", "inspection location"), ("equipment", "equipment identifier")):
        answers[field] = chat(
            base, f"For synthetic handheld mission {state['run_id']}, "
            f"what is the exact {label}? Return the identifier from the documents."
        )
        if state[field].lower() not in answers[field].lower():
            (output / "rag.json").write_text(json.dumps(answers, indent=2))
            raise RuntimeError(f"Document-only {field} was not recovered")
    answers["brief"] = chat(
        base, f"Extract a concise brief for synthetic handheld mission {state['run_id']}. "
        "Include objective, deadline, location, unit, restrictions, equipment, dependencies "
        "and known risks only when explicitly stated. Do not invent names or missing facts."
    )
    (output / "rag.json").write_text(json.dumps(answers, indent=2))
    if state["location"].lower() not in answers["brief"].lower() or "14:35" not in answers["brief"]:
        raise RuntimeError("Mission brief omitted known location or deadline")
    print("PASS: PDF/DOCX-only identifiers recovered and grounded mission brief generated")


def general(base, output):
    before = documents(base)
    state = json.loads((output / "state.json").read_text())
    if not all(name in before for name in state["documents"]):
        raise RuntimeError("General-chat smoke requires the ingested mission documents to remain present")
    answer = chat(base, "What is RTSP? Give a concise general-knowledge answer. "
                  "Begin with 'General knowledge:'. Do not mention or attribute this answer "
                  "to any mission, document, or retrieved context.")
    after = documents(base)
    (output / "general.json").write_text(json.dumps(
        {"answer": answer, "documents_before": before, "documents_after": after}, indent=2
    ))
    if sorted(before) != sorted(after):
        raise RuntimeError("Corpus changed during general conversation")
    if not answer.startswith("General knowledge:"):
        raise RuntimeError("General answer did not identify its knowledge basis")
    if not re.search(r"real[- ]time streaming protocol", answer, re.IGNORECASE):
        raise RuntimeError("General answer did not explain RTSP correctly")
    if re.search(r"\b(mission|document|retrieved)\b", answer, re.IGNORECASE):
        raise RuntimeError("General answer unexpectedly mentions mission retrieval")
    print("PASS: stock /chat answered general question while mission corpus remained intact")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("ingest", "rag", "general"))
    args = parser.parse_args()
    base = os.environ.get("CHATQNA_URL", "http://127.0.0.1:8888").rstrip("/")
    output = Path(os.environ.get("CHATQNA_EVIDENCE", "docs/application/evidence/chatqna/smoke"))
    output.mkdir(parents=True, exist_ok=True)
    model = api(base, "/model")
    if model.get("llm_model") != "Intel/neural-chat-7b-v3-3":
        raise RuntimeError(f"Unexpected configured LLM: {model}")
    {"ingest": ingest, "rag": rag, "general": general}[args.mode](base, output)


if __name__ == "__main__":
    main()
