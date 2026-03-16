import pdfplumber


def get_pdf_text(path):
    texts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            if page_text:
                texts.append(page_text)
    return "\n".join(texts)

if __name__ == "__main__":
    text = get_pdf_text("/Users/hekaiyu/Desktop/project/python/read_paper/file/analysis/paper.pdf")
    print(text)
