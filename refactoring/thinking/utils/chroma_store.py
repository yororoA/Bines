from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from thinking_settings import thinking_settings
from langchain_core.documents import Document
from langchain.messages import AnyMessage
from datetime import datetime


_embeddings = HuggingFaceEmbeddings(
    model_name=thinking_settings.RAG_EMBEDDING_MODEL,
)

_vectorstore = Chroma(
    collection_name="conversation_history",
    embedding_function=_embeddings,
    persist_directory="./chroma_db",
)

_retriever = _vectorstore.as_retriever(search_kwargs={"k": 5})


def add_conversation(messages: list[AnyMessage], metadata: dict | None = None):
    docs = []
    for msg in messages:
        doc = Document(
            page_content=f"{msg.type}: {msg.content}",
            metadata={
                "message_type": msg.type,
                "timestamp": datetime.now().isoformat(),
                **(metadata or {}),
            },
        )
        docs.append(doc)
    if docs:
        _vectorstore.add_documents(docs)


def add_text(text: str, metadata: dict | None = None):
    doc = Document(
        page_content=text,
        metadata={
            "timestamp": datetime.now().isoformat(),
            **(metadata or {}),
        },
    )
    _vectorstore.add_documents([doc])


def retrieve_relevant(query: str, k: int = 5) -> list[Document]:
    return _retriever.invoke(query)


def retrieve_relevant_str(query: str, k: int = 5) -> str:
    docs = retrieve_relevant(query, k)
    if not docs:
        return ""
    return "\n\n---\n\n".join(
        f"[{doc.metadata.get('timestamp', 'unknown')}] {doc.page_content}"
        for doc in docs
    )


def get_vectorstore() -> Chroma:
    return _vectorstore


def get_retriever():
    return _retriever
