import glob
import os

import numpy as np
import ollama

import config

# A tiny RAG retriever (the same idea as your resume pipeline, without LangChain):
# chunk the policy docs, embed each chunk once, then return the chunks closest
# in meaning to a query. The docs are small, so everything stays in memory.


def load_chunks():
    """Split each policy file on blank lines, merging paragraphs up to CHUNK_SIZE."""
    chunks = []
    for path in sorted(glob.glob(os.path.join(config.POLICY_DIR, "*.md"))):
        with open(path, encoding="utf-8") as f:
            paragraphs = [p.strip() for p in f.read().split("\n\n") if p.strip()]
        title = paragraphs[0].lstrip("# ")
        current = ""
        for p in paragraphs:
            if current and len(current) + len(p) > config.CHUNK_SIZE:
                chunks.append({"source": os.path.basename(path), "text": current.strip()})
                current = f"# {title}\n"  # keep the doc title on every chunk
            current += p + "\n\n"
        chunks.append({"source": os.path.basename(path), "text": current.strip()})
    return chunks


def embed(texts):
    vectors = np.array(ollama.embed(model=config.EMBEDDING_MODEL, input=texts).embeddings)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


class PolicySearch:
    def __init__(self):
        self.chunks = load_chunks()
        self.vectors = embed([c["text"] for c in self.chunks])

    def search(self, query, k=config.TOP_K):
        scores = self.vectors @ embed([query])[0]  # cosine similarity
        best = np.argsort(scores)[::-1][:k]
        return [{**self.chunks[i], "score": round(float(scores[i]), 3)} for i in best]


if __name__ == "__main__":
    import sys

    query = " ".join(sys.argv[1:]) or "my train was 45 minutes late"
    for hit in PolicySearch().search(query):
        print(f"--- {hit['source']} (score {hit['score']})\n{hit['text']}\n")
