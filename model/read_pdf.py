import pdfplumber
def get_pdf_text(path):
    text = ""
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text += page.extract_text()  # 提取文本
    return text

if __name__ == "__main__":
    text = get_pdf_text("/Users/hekaiyu/Desktop/project/python/read_paper/file/analysis/paper.pdf")
    print(text)