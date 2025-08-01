import numpy as np
import os
import time
import uuid
import logging
from typing import Optional, Union, Dict, List
import concurrent.futures
from tqdm import tqdm

from dsrag.dsparse.main import parse_and_chunk
from dsrag.add_document import (
    auto_context, 
    get_embeddings, 
    add_chunks_to_db, 
    add_vectors_to_db,
)
from dsrag.auto_context import get_segment_header
from dsrag.rse import (
    get_relevance_values,
    get_best_segments,
    get_meta_document,
    RSE_PARAMS_PRESETS,
)
from dsrag.database.vector import Vector, VectorDB, BasicVectorDB
from dsrag.database.vector.types import MetadataFilter
from dsrag.database.chunk import ChunkDB, BasicChunkDB
from dsrag.embedding import Embedding, OpenAIEmbedding
from dsrag.reranker import Reranker, CohereReranker
from dsrag.llm import LLM, OpenAIChatAPI
from dsrag.dsparse.file_parsing.file_system import FileSystem, LocalFileSystem
from dsrag.metadata import MetadataStorage, LocalMetadataStorage
from dsrag.chat.citations import convert_elements_to_page_content

class KnowledgeBase:
    def __init__(
        self,
        kb_id: str,
        title: str = "",
        supp_id: str = "",
        description: str = "",
        language: str = "en",
        storage_directory: str = "~/dsRAG",
        embedding_model: Optional[Embedding] = None,
        reranker: Optional[Reranker] = None,
        auto_context_model: Optional[LLM] = None,
        vector_db: Optional[VectorDB] = None,
        chunk_db: Optional[ChunkDB] = None,
        file_system: Optional[FileSystem] = None,
        exists_ok: bool = True,
        save_metadata_to_disk: bool = True,
        metadata_storage: Optional[MetadataStorage] = None
    ):
        """Initialize a KnowledgeBase instance.

        Args:
            kb_id (str): Unique identifier for the knowledge base.
            title (str, optional): Title of the knowledge base. Defaults to "".
            supp_id (str, optional): Supplementary identifier. Defaults to "".
            description (str, optional): Description of the knowledge base. Defaults to "".
            language (str, optional): Language code for the knowledge base. Defaults to "en".
            storage_directory (str, optional): Base directory for storing files. Defaults to "~/dsRAG".
            embedding_model (Optional[Embedding], optional): Model for generating embeddings. 
                Defaults to OpenAIEmbedding.
            reranker (Optional[Reranker], optional): Model for reranking results. 
                Defaults to CohereReranker.
            auto_context_model (Optional[LLM], optional): LLM for generating context. 
                Defaults to OpenAIChatAPI.
            vector_db (Optional[VectorDB], optional): Vector database for storing embeddings. 
                Defaults to BasicVectorDB.
            chunk_db (Optional[ChunkDB], optional): Database for storing text chunks. 
                Defaults to BasicChunkDB.
            file_system (Optional[FileSystem], optional): File system for storing images. 
                Defaults to LocalFileSystem.
            exists_ok (bool, optional): Whether to load existing KB if it exists. Defaults to True.
            save_metadata_to_disk (bool, optional): Whether to persist metadata. Defaults to True.
            metadata_storage (Optional[MetadataStorage], optional): Storage for KB metadata. 
                Defaults to LocalMetadataStorage.

        Raises:
            ValueError: If KB exists and exists_ok is False.
        """
        self.kb_id = kb_id
        self.storage_directory = os.path.expanduser(storage_directory)
        self.metadata_storage = metadata_storage if metadata_storage else LocalMetadataStorage(self.storage_directory)

        if save_metadata_to_disk:
            # load the KB if it exists; otherwise, initialize it and save it to disk
            if self.metadata_storage.kb_exists(self.kb_id) and exists_ok:
                self._load(
                    auto_context_model, reranker, file_system, chunk_db, vector_db
                )
                self._save()
            elif self.metadata_storage.kb_exists(self.kb_id) and not exists_ok:
                raise ValueError(
                    f"Knowledge Base with ID {kb_id} already exists. Use exists_ok=True to load it."
                )
            else:
                created_time = int(time.time())
                self.kb_metadata = {
                    "title": title,
                    "description": description,
                    "language": language,
                    "supp_id": supp_id,
                    "created_on": created_time,
                }
                self._initialize_components(
                    embedding_model, reranker, auto_context_model, vector_db, chunk_db, file_system
                )
                self._save()  # save the config for the KB to disk
        else:
            self.kb_metadata = {
                "title": title,
                "description": description,
                "language": language,
                "supp_id": supp_id,
            }
            self._initialize_components(
                embedding_model, reranker, auto_context_model, vector_db, chunk_db, file_system
            )

    def _get_metadata_path(self):
        """Get the path to the metadata file.

        Returns:
            str: Path to the metadata JSON file.
        """
        return os.path.join(self.storage_directory, "metadata", f"{self.kb_id}.json")

    def _initialize_components(
        self,
        embedding_model: Optional[Embedding],
        reranker: Optional[Reranker],
        auto_context_model: Optional[LLM],
        vector_db: Optional[VectorDB],
        chunk_db: Optional[ChunkDB],
        file_system: Optional[FileSystem],
    ):
        """Initialize the knowledge base components.

        Internal method to set up embedding model, reranker, databases, etc.
        """
        self.embedding_model = embedding_model if embedding_model else OpenAIEmbedding()
        self.reranker = reranker if reranker else CohereReranker()
        self.auto_context_model = (
            auto_context_model if auto_context_model else OpenAIChatAPI()
        )
        self.vector_db = (
            vector_db
            if vector_db
            else BasicVectorDB(self.kb_id, self.storage_directory)
        )
        self.chunk_db = (
            chunk_db if chunk_db else BasicChunkDB(self.kb_id, self.storage_directory)
        )
        self.file_system = file_system if file_system else LocalFileSystem(base_path=os.path.join(self.storage_directory, "page_images"))
        self.vector_dimension = self.embedding_model.dimension

    def _save(self):
        """Save the knowledge base configuration to disk.

        Internal method to serialize components and metadata.
        """
        # Serialize components
        components = {
            "embedding_model": self.embedding_model.to_dict(),
            "reranker": self.reranker.to_dict(),
            "auto_context_model": self.auto_context_model.to_dict(),
            "vector_db": self.vector_db.to_dict(),
            "chunk_db": self.chunk_db.to_dict(),
            "file_system": self.file_system.to_dict(),
        }
        # Combine metadata and components
        full_data = {**self.kb_metadata, "components": components}

        self.metadata_storage.save(full_data, self.kb_id)

    def _load(self, auto_context_model=None, reranker=None, file_system=None, chunk_db=None, vector_db=None):
        """Load a knowledge base configuration from disk.

        Internal method to deserialize components and metadata.

        Args:
            auto_context_model (Optional[LLM], optional): Override stored AutoContext model.
            reranker (Optional[Reranker], optional): Override stored reranker model.
            file_system (Optional[FileSystem], optional): Override stored file system.
            chunk_db (Optional[ChunkDB], optional): Override stored chunk database.
            vector_db (Optional[VectorDB], optional): Override stored vector database.

        Note:
            Only auto_context_model and reranker can safely override stored components.
            Other component overrides may break functionality if not compatible.
        """
        data = self.metadata_storage.load(self.kb_id)
        self.kb_metadata = {
            key: value for key, value in data.items() if key != "components"
        }
        components = data.get("components", {})
        # Deserialize components
        self.embedding_model = Embedding.from_dict(
            components.get("embedding_model", {}))
        
        self.reranker = (
            reranker
            if reranker
            else Reranker.from_dict(components.get("reranker", {}))
        )
        self.auto_context_model = (
            auto_context_model
            if auto_context_model
            else LLM.from_dict(components.get("auto_context_model", {}))
        )
        # Log warnings for overridden components
        base_extra = {"kb_id": self.kb_id}
        if vector_db is not None:
            logging.warning(f"Overriding stored vector_db for KB '{self.kb_id}' during load.", extra=base_extra)
        self.vector_db = (
            vector_db
            if vector_db
            else VectorDB.from_dict(components.get("vector_db", {}))
        )
        if chunk_db is not None:
            logging.warning(f"Overriding stored chunk_db for KB '{self.kb_id}' during load.", extra=base_extra)
            self.chunk_db = chunk_db
        else:
            self.chunk_db = ChunkDB.from_dict(components.get("chunk_db", {}))

        file_system_dict = components.get("file_system", None)

        if file_system is not None:
            logging.warning(f"Overriding stored file_system for KB '{self.kb_id}' during load.", extra=base_extra)
            # If the file system does not exist but is provided, use the provided file system
            self.file_system = file_system
        elif file_system_dict is not None:
            # If the file system dict exists, use it
            self.file_system = FileSystem.from_dict(file_system_dict)
        else:
            # If the file system does not exist and is not provided, default to LocalFileSystem
            self.file_system = LocalFileSystem(base_path=self.storage_directory)

        self.vector_dimension = self.embedding_model.dimension

    def delete(self):
        """Delete the knowledge base and all associated data.

        Removes all documents, vectors, chunks, and metadata associated with this KB.
        """
        # delete all documents in the KB
        doc_ids_to_delete = self.chunk_db.get_all_doc_ids()
        for doc_id in doc_ids_to_delete:
            self.delete_document(doc_id)

        self.chunk_db.delete()
        self.vector_db.delete()
        self.file_system.delete_kb(self.kb_id)

        # delete the metadata file
        self.metadata_storage.delete(self.kb_id)

    def add_document(
        self,
        doc_id: str,
        text: str = "",
        file_path: str = "",
        document_title: str = "",
        auto_context_config: dict = {},
        file_parsing_config: dict = {},
        semantic_sectioning_config: dict = {},
        chunking_config: dict = {},
        chunk_size: int = None,
        min_length_for_chunking: int = None,
        supp_id: str = "",
        metadata: dict = {},
    ):
        """Add a document to the knowledge base.

        This method processes and adds a document to the knowledge base. The document can be provided
        either as text or as a file path. The document will be processed according to the provided
        configuration parameters.

        Args:
            doc_id (str): Unique identifier for the document. A file name or path is a good choice.
            text (str, optional): The full text of the document. Defaults to "".
            file_path (str, optional): Path to the file to be uploaded. Supported file types are
                .txt, .md, .pdf, and .docx. Defaults to "".
            document_title (str, optional): The title of the document. If not provided, either the
                doc_id or an LLM-generated title will be used, depending on auto_context_config.
                Defaults to "".
            auto_context_config (dict, optional): Configuration parameters for AutoContext. Example:
                ```python
                {
                    # Whether to use an LLM-generated title if no title is provided
                    "use_generated_title": True,
                    
                    # Guidance for generating the document title
                    "document_title_guidance": "Generate a concise title",
                    
                    # Whether to get a document summary
                    "get_document_summary": True,
                    
                    # Guidance for document summarization
                    "document_summarization_guidance": "Summarize key points",
                    
                    # Whether to get section summaries
                    "get_section_summaries": False,

                    # Guidance for section summarization
                    "section_summarization_guidance": "Summarize each section",

                    # Maximum concurrent requests for section summarization
                    "llm_max_concurrent_requests": 5,
                    
                    # Minimum average characters per section in a window (default: 500)
                    # If sections average fewer chars than this, they'll be consolidated
                    "min_avg_chars_per_section": 500,

                    # Custom term mappings (key: term to map to, value: list of terms to map from)
                    "custom_term_mapping": {
                        "AI": ["artificial intelligence", "machine learning"],
                        "ML": ["machine learning", "deep learning"]
                    }
                }
                ```
            file_parsing_config (dict, optional): Configuration parameters for file parsing. Example:
                ```python
                {
                    # Whether to use VLM for parsing
                    "use_vlm": False,
                    
                    # VLM configuration (ignored if use_vlm is False)
                    "vlm_config": {
                        # VLM provider (currently only "gemini" and "vertex_ai" supported)
                        "provider": "vertex_ai",
                        
                        # The VLM model to use
                        "model": "model_name",
                        
                        # GCP project ID (required for "vertex_ai")
                        "project_id": "your-project-id",
                        
                        # GCP location (required for "vertex_ai")
                        "location": "us-central1",
                        
                        # Path to save intermediate files
                        "save_path": "/path/to/save",
                        
                        # Element types to exclude
                        "exclude_elements": ["Header", "Footer"],
                        
                        # Whether images are pre-extracted
                        "images_already_exist": False
                    },
                    
                    # Save images even if VLM unused
                    "always_save_page_images": False
                }
                ```
            semantic_sectioning_config (dict, optional): Configuration for semantic sectioning. Example:
                ```python
                {
                    # LLM provider for semantic sectioning
                    "llm_provider": "openai",  # or "anthropic" or "gemini"
                    
                    # LLM model to use
                    "model": "gpt-4o-mini",
                    
                    # Whether to use semantic sectioning
                    "use_semantic_sectioning": True
                }
                ```
            chunking_config (dict, optional): Configuration for document/section chunking. Example:
                ```python
                {
                    # Maximum characters per chunk
                    "chunk_size": 800,
                    
                    # Minimum text length to allow chunking
                    "min_length_for_chunking": 2000
                }
                ```
            supp_id (str, optional): Supplementary identifier. Defaults to "".
            metadata (dict, optional): Additional metadata for the document. Defaults to {}.

        Note:
            Either text or file_path must be provided. If both are provided, text takes precedence.
            The document processing flow is:
            1. File parsing (if file_path provided)
            2. Semantic sectioning (if enabled)
            3. Chunking
            4. AutoContext
            5. Embedding
            6. Storage in vector and chunk databases
        """
        # Get a logger specific to ingestion operations
        ingestion_logger = logging.getLogger("dsrag.ingestion")
        
        # Create a dictionary with base log context fields
        base_extra = {"kb_id": self.kb_id, "doc_id": doc_id}
        if file_path:
            base_extra["file_path"] = file_path

        # Log start of ingestion at INFO level
        ingestion_logger.info("Starting document ingestion", extra=base_extra)
        
        # Log configuration parameters at DEBUG level
        config_extra = {
            **base_extra,
            "provided_auto_context_config": auto_context_config,
            "provided_file_parsing_config": file_parsing_config,
            "provided_semantic_sectioning_config": semantic_sectioning_config,
            "provided_chunking_config": chunking_config,
            "metadata": metadata
        }
        ingestion_logger.debug("Ingestion parameters", extra=config_extra)
        
        # Start timing the overall ingestion process
        overall_start_time = time.perf_counter()

        try:
            # Handle the backwards compatibility for chunk_size and min_length_for_chunking
            if chunk_size is not None:
                chunking_config["chunk_size"] = chunk_size
            if min_length_for_chunking is not None:
                chunking_config["min_length_for_chunking"] = min_length_for_chunking
            
            # verify that either text or file_path is provided
            if text == "" and file_path == "":
                raise ValueError("Either text or file_path must be provided")

            # verify that the document does not already exist in the KB - the doc_id should be unique
            if doc_id in self.chunk_db.get_all_doc_ids():
                ingestion_logger.warning(
                    "Document already exists in knowledge base, skipping", 
                    extra=base_extra
                )
                return
            
            # verify the doc_id is valid
            if "/" in doc_id:
                raise ValueError("doc_id cannot contain '/' characters")
            
            # --- Parsing and Chunking Step ---
            step_start_time = time.perf_counter()
            sections, chunks = parse_and_chunk(
                kb_id=self.kb_id,
                doc_id=doc_id,
                file_path=file_path, 
                text=text, 
                file_parsing_config=file_parsing_config, 
                semantic_sectioning_config=semantic_sectioning_config, 
                chunking_config=chunking_config,
                file_system=self.file_system,
            )
            step_duration = time.perf_counter() - step_start_time
            ingestion_logger.debug("Parsing and Chunking complete", extra={
                **base_extra, 
                "step": "parse_chunk", 
                "duration_s": round(step_duration, 4),
                "num_sections": len(sections), 
                "num_chunks": len(chunks)
            })

            # construct full document text from sections (for auto_context)
            document_text = ""
            for section in sections:
                document_text += section["content"]

            # --- AutoContext Step ---
            chunks, chunks_to_embed = auto_context(
                kb_id=self.kb_id,
                auto_context_model=self.auto_context_model,
                sections=sections,
                chunks=chunks,
                text=document_text,
                doc_id=doc_id,
                document_title=document_title,
                auto_context_config=auto_context_config,
                language=self.kb_metadata.get("language", "en"),
            )
            
            # --- Embedding Step ---
            step_start_time = time.perf_counter()
            chunk_embeddings = get_embeddings(
                embedding_model=self.embedding_model,
                chunks_to_embed=chunks_to_embed,
            )
            step_duration = time.perf_counter() - step_start_time
            ingestion_logger.debug("Embedding complete", extra={
                **base_extra, 
                "step": "embedding", 
                "duration_s": round(step_duration, 4),
                "num_embeddings": len(chunk_embeddings), 
                "model": self.embedding_model.__class__.__name__
            })
            
            # --- DB Storage Step ---
            step_start_time = time.perf_counter()
            add_chunks_to_db(
                chunk_db=self.chunk_db,
                chunks=chunks,
                chunks_to_embed=chunks_to_embed,
                chunk_embeddings=chunk_embeddings,
                metadata=metadata,
                doc_id=doc_id,
                supp_id=supp_id
            )
            add_vectors_to_db(
                vector_db=self.vector_db,
                chunks=chunks,
                chunk_embeddings=chunk_embeddings,
                metadata=metadata,
                doc_id=doc_id,
            )
            step_duration = time.perf_counter() - step_start_time
            ingestion_logger.debug("Database storage complete", extra={
                **base_extra,
                "step": "db_storage", 
                "duration_s": round(step_duration, 4),
                "vector_db": self.vector_db.__class__.__name__,
                "chunk_db": self.chunk_db.__class__.__name__
            })

            # Convert elements to page content if the document was processed with page numbers
            if file_path and file_parsing_config.get('use_vlm', False):
                try:
                    step_start_time = time.perf_counter()
                    elements = self.file_system.load_data(kb_id=self.kb_id, doc_id=doc_id, data_name="elements")
                    if elements:
                        convert_elements_to_page_content(
                            elements=elements,
                            kb_id=self.kb_id,
                            doc_id=doc_id,
                            file_system=self.file_system
                        )
                    step_duration = time.perf_counter() - step_start_time
                    ingestion_logger.debug("Page content conversion complete", extra={
                        **base_extra,
                        "step": "page_content", 
                        "duration_s": round(step_duration, 4),
                        "num_elements": len(elements) if elements else 0
                    })
                except Exception as e:
                    ingestion_logger.warning(
                        "Failed to load or process elements for page content", 
                        extra={**base_extra, "error": str(e)}
                    )

            self._save()  # save to disk after adding a document
            
            # Log successful completion with total duration
            overall_duration = time.perf_counter() - overall_start_time
            ingestion_logger.info("Document ingestion successful", extra={
                **base_extra,
                "total_duration_s": round(overall_duration, 4)
            })
            
        except Exception as e:
            # Log error with exception info
            overall_duration = time.perf_counter() - overall_start_time
            ingestion_logger.error(
                "Document ingestion failed", 
                extra={
                    **base_extra,
                    "total_duration_s": round(overall_duration, 4),
                    "error": str(e)
                },
                exc_info=True
            )
            # Re-raise the exception
            raise

    def add_documents(
        self,
        documents: List[Dict[str, Union[str, dict]]],
        max_workers: int = 1,
        show_progress: bool = True,
        rate_limit_pause: float = 1.0,
    ) -> List[str]:
        """Add multiple documents to the knowledge base in parallel.
        
        Args:
            documents (List[Dict[str, Union[str, dict]]]): List of document dictionaries. Each must contain:
                - 'doc_id' (str): Unique identifier for the document
                And either:
                - 'text' (str): The document content, or
                - 'file_path' (str): Path to the document file
                Optional keys:
                - 'document_title' (str): Document title
                - 'auto_context_config' (dict): AutoContext configuration
                - 'file_parsing_config' (dict): File parsing configuration
                - 'semantic_sectioning_config' (dict): Semantic sectioning configuration
                - 'chunking_config' (dict): Chunking configuration
                - 'supp_id' (str): Supplementary identifier
                - 'metadata' (dict): Additional metadata
            max_workers (int, optional): Maximum number of worker threads. Defaults to 1.
            show_progress (bool, optional): Whether to show a progress bar. Defaults to True.
            rate_limit_pause (float, optional): Pause between uploads in seconds. Defaults to 1.0.

        Returns:
            List[str]: List of successfully uploaded document IDs.

        Note:
            Be sure to use thread-safe VectorDB and ChunkDB implementations when max_workers > 1.
            The default implementations (BasicVectorDB and BasicChunkDB) are not thread-safe.
        """
        successful_uploads = []
        
        def process_document(doc: Dict) -> Optional[str]:
            try:
                # Extract required parameters
                doc_id = doc['doc_id']
                print(f"Starting to process document: {doc_id}")  # Debug log
                
                # Create a copy of the document dict to avoid modification during iteration
                doc_params = doc.copy()
                
                # Extract required parameters from the copy
                text = doc_params.get('text', '')
                file_path = doc_params.get('file_path', '')
                
                # Extract optional parameters with defaults
                document_title = doc_params.get('document_title', '')
                auto_context_config = doc_params.get('auto_context_config', {}).copy()
                file_parsing_config = doc_params.get('file_parsing_config', {}).copy()
                semantic_sectioning_config = doc_params.get('semantic_sectioning_config', {}).copy()
                chunking_config = doc_params.get('chunking_config', {}).copy()
                supp_id = doc_params.get('supp_id', '')
                metadata = doc_params.get('metadata', {}).copy()
                
                print(f"Extracted parameters for {doc_id}")  # Debug log
                
                # Call add_document with extracted parameters
                self.add_document(
                    doc_id=doc_id,
                    text=text,
                    file_path=file_path,
                    document_title=document_title,
                    auto_context_config=auto_context_config,
                    file_parsing_config=file_parsing_config,
                    semantic_sectioning_config=semantic_sectioning_config,
                    chunking_config=chunking_config,
                    supp_id=supp_id,
                    metadata=metadata
                )
                
                print(f"Successfully processed document: {doc_id}")  # Debug log
                
                # Pause to avoid rate limits
                time.sleep(rate_limit_pause)
                return doc_id
                
            except Exception as e:
                import traceback
                error_msg = f"Error processing document {doc.get('doc_id', 'unknown')}:\n"
                error_msg += f"Error type: {type(e).__name__}\n"
                error_msg += f"Error message: {str(e)}\n"
                error_msg += "Traceback:\n"
                error_msg += traceback.format_exc()
                print(error_msg)
                return None

        # Process documents in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Create futures
            future_to_doc = {
                executor.submit(process_document, doc): doc 
                for doc in documents
            }
            
            # Process results with optional progress bar
            if show_progress:
                futures = tqdm(
                    concurrent.futures.as_completed(future_to_doc),
                    total=len(documents),
                    desc="Processing documents"
                )
            else:
                futures = concurrent.futures.as_completed(future_to_doc)
                
            for future in futures:
                doc_id = future.result()
                if doc_id:
                    successful_uploads.append(doc_id)
        
        return successful_uploads

    def delete_document(self, doc_id: str):
        """Delete a document from the knowledge base.

        Args:
            doc_id (str): ID of the document to delete.
        """
        self.chunk_db.remove_document(doc_id)
        self.vector_db.remove_document(doc_id)
        self.file_system.delete_directory(self.kb_id, doc_id)

    def _get_chunk_text(self, doc_id: str, chunk_index: int) -> Optional[str]:
        """Get the text content of a specific chunk.

        Internal method to retrieve chunk text from the chunk database.
        """
        return self.chunk_db.get_chunk_text(doc_id, chunk_index)
    
    def _get_is_visual(self, doc_id: str, chunk_index: int) -> bool:
        """Check if a chunk contains visual content.

        Internal method to check chunk type.
        """
        return self.chunk_db.get_is_visual(doc_id, chunk_index)
    
    def _get_chunk_content(self, doc_id: str, chunk_index: int) -> tuple[str, str]:
        """Get the full content of a specific chunk.

        Internal method to retrieve chunk content.
        """
        chunk_text = self.chunk_db.get_chunk_text(doc_id, chunk_index)
        return chunk_text

    def _get_segment_header(self, doc_id: str, chunk_index: int) -> str:
        """Generate a header for a segment.

        Internal method to create segment headers.
        """
        document_title = self.chunk_db.get_document_title(doc_id, chunk_index) or ""
        document_summary = self.chunk_db.get_document_summary(doc_id, chunk_index) or ""
        return get_segment_header(
            document_title=document_title, document_summary=document_summary
        )

    def _get_embeddings(self, text: list[str], input_type: str = "") -> list[Vector]:
        """Generate embeddings for text.

        Internal method to interface with embedding model.
        """
        return self.embedding_model.get_embeddings(text, input_type)

    def _cosine_similarity(self, v1, v2):
        """Calculate cosine similarity between vectors.

        Internal method for vector similarity calculation.
        """
        return np.dot(v1, v2)

    def _search(self, query: str, top_k: int, metadata_filter: Optional[MetadataFilter] = None) -> list:
        """Search the knowledge base for relevant chunks.

        Internal method for single query search.
        """
        query_vector = self._get_embeddings([query], input_type="query")[0]
        search_results = self.vector_db.search(query_vector, top_k, metadata_filter)
        if len(search_results) == 0:
            return []
        search_results = self.reranker.rerank_search_results(query, search_results)
        return search_results

    def _get_all_ranked_results(self, search_queries: list[str], metadata_filter: Optional[MetadataFilter] = None):
        """Execute multiple search queries.

        Internal method for parallel query execution.
        """
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [executor.submit(self._search, query, 200, metadata_filter) for query in search_queries]
            all_ranked_results = []
            for future in futures:
                ranked_results = future.result()
                all_ranked_results.append(ranked_results)
        return all_ranked_results
    
    def _get_segment_page_numbers(self, doc_id: str, chunk_start: int, chunk_end: int) -> tuple:
        """Get page numbers for a segment.

        Internal method for page number lookup.
        """
        start_page_number, _ = self.chunk_db.get_chunk_page_numbers(doc_id, chunk_start)
        _, end_page_number = self.chunk_db.get_chunk_page_numbers(doc_id, chunk_end - 1)
        return start_page_number, end_page_number
    
    def _get_segment_content_from_database(self, doc_id: str, chunk_start: int, chunk_end: int, return_mode: str):
        """Retrieve segment content from database.

        Internal method for content retrieval.
        """
        assert return_mode in ["text", "page_images", "dynamic"]

        if return_mode == "dynamic":
            # loop through the chunks in the segment to see if any of them are visual
            segment_is_visual = False
            for chunk_index in range(chunk_start, chunk_end):
                is_visual = self._get_is_visual(doc_id, chunk_index)
                if is_visual:
                    segment_is_visual = True
                    break

            # set the return mode based on whether the segment contains visual content or not
            if segment_is_visual:
                return_mode = "page_images"
            else:
                return_mode = "text"

        if return_mode == "text":
            segment_text = f"{self._get_segment_header(doc_id=doc_id, chunk_index=chunk_start)}\n\n"  # initialize the segment with the segment header
            for chunk_index in range(chunk_start, chunk_end):
                chunk_text = self._get_chunk_text(doc_id, chunk_index) or ""
                segment_text += chunk_text
            return segment_text.strip()
        else:
            # get the page numbers that the segment starts and ends on
            start_page_number, end_page_number = self._get_segment_page_numbers(doc_id, chunk_start, chunk_end)
            page_image_paths = self.file_system.get_files(kb_id=self.kb_id, doc_id=doc_id, page_start=start_page_number, page_end=end_page_number)
            # If there are no page images, fallback to using text mode
            if page_image_paths == []:
                page_image_paths = self._get_segment_content_from_database(doc_id, chunk_start, chunk_end, return_mode="text")
            return page_image_paths

    def query(
        self,
        search_queries: list[str],
        rse_params: Union[Dict, str] = "balanced",
        latency_profiling: bool = False,
        metadata_filter: Optional[MetadataFilter] = None,
        return_mode: str = "text",
    ) -> list[dict]:
        """Query the knowledge base to retrieve relevant segments.

        Args:
            search_queries (list[str]): List of search queries to execute.
            rse_params (Union[Dict, str], optional): RSE parameters or preset name. Example:
                ```python
                {
                    # Maximum segment length in chunks
                    "max_length": 5,
                    
                    # Maximum total length of all segments
                    "overall_max_length": 20,
                    
                    # Minimum relevance value for segments
                    "minimum_value": 0.5,
                    
                    # Penalty for irrelevant chunks (0-1)
                    "irrelevant_chunk_penalty": 0.8,
                    
                    # Length increase per additional query
                    "overall_max_length_extension": 5,
                    
                    # Rate at which relevance decays
                    "decay_rate": 0.1,
                    
                    # Number of documents to consider
                    "top_k_for_document_selection": 10,
                    
                    # Whether to scale by chunk length
                    "chunk_length_adjustment": True
                }
                ```
                Alternatively, use preset names: "balanced" (default), "precise", or "comprehensive"
            latency_profiling (bool, optional): Whether to print timing info. Defaults to False.
            metadata_filter (Optional[MetadataFilter], optional): Filter for document selection. 
                Defaults to None.
            return_mode (str, optional): Content return format. One of:
                - "text": Return segments as text
                - "page_images": Return list of page image paths
                - "dynamic": Choose format based on content type
                Defaults to "text".

        Returns:
            list[dict]: List of segment information dictionaries, ordered by relevance.
                Each dictionary contains:
                ```python
                {
                    # Document identifier
                    "doc_id": "example_doc",
                    
                    # Starting chunk index
                    "chunk_start": 0,
                    
                    # Ending chunk index (exclusive)
                    "chunk_end": 5,
                    
                    # Segment content (text or image paths)
                    "content": "Example text content...",
                    
                    # Starting page number
                    "segment_page_start": 1,
                    
                    # Ending page number
                    "segment_page_end": 3,
                    
                    # Relevance score
                    "score": 0.95
                }
                ```
        """
        # Get a logger specific to query operations
        query_logger = logging.getLogger("dsrag.query")
        
        # Generate a unique query ID
        query_id = str(uuid.uuid4())
        
        # Create a dictionary with base log context fields
        base_extra = {"kb_id": self.kb_id, "query_id": query_id}
        
        # Log start of query operation at INFO level
        query_logger.info("Starting query", extra={
            **base_extra, 
            "num_search_queries": len(search_queries)
        })
        
        # Start timing the overall query process
        overall_start_time = time.perf_counter()

        try:
            # Log query parameters at DEBUG level
            query_logger.debug("Query parameters", extra={
                **base_extra,
                "search_queries": search_queries,
                "rse_params": rse_params if isinstance(rse_params, dict) else {"preset": rse_params},
                "metadata_filter": metadata_filter,
                "return_mode": return_mode,
                "reranker_model": self.reranker.__class__.__name__
            })
            
            # check if the rse_params is a preset name and convert it to a dictionary if it is
            if isinstance(rse_params, str) and rse_params in RSE_PARAMS_PRESETS:
                rse_params = RSE_PARAMS_PRESETS[rse_params]
            elif isinstance(rse_params, str):
                raise ValueError(f"Invalid rse_params preset name: {rse_params}")

            # set the RSE parameters - use the 'balanced' preset as the default for any missing parameters
            default_rse_params = RSE_PARAMS_PRESETS["balanced"]
            max_length = rse_params.get("max_length", default_rse_params["max_length"])
            overall_max_length = rse_params.get(
                "overall_max_length", default_rse_params["overall_max_length"]
            )
            minimum_value = rse_params.get(
                "minimum_value", default_rse_params["minimum_value"]
            )
            irrelevant_chunk_penalty = rse_params.get(
                "irrelevant_chunk_penalty", default_rse_params["irrelevant_chunk_penalty"]
            )
            overall_max_length_extension = rse_params.get(
                "overall_max_length_extension",
                default_rse_params["overall_max_length_extension"],
            )
            decay_rate = rse_params.get("decay_rate", default_rse_params["decay_rate"])
            top_k_for_document_selection = rse_params.get(
                "top_k_for_document_selection",
                default_rse_params["top_k_for_document_selection"],
            )
            chunk_length_adjustment = rse_params.get(
                "chunk_length_adjustment", default_rse_params["chunk_length_adjustment"]
            )

            overall_max_length += (
                len(search_queries) - 1
            ) * overall_max_length_extension  # increase the overall max length for each additional query

            # --- Search/Rerank Step ---
            step_start_time = time.perf_counter()
            all_ranked_results = self._get_all_ranked_results(search_queries=search_queries, metadata_filter=metadata_filter)
            step_duration = time.perf_counter() - step_start_time
            
            # Get the number of initial results per query
            initial_results_per_query = [len(results) for results in all_ranked_results]
            
            # Log information about search/rerank step
            query_logger.debug("Search/Rerank complete", extra={
                **base_extra, 
                "step": "search_rerank", 
                "duration_s": round(step_duration, 4),
                "num_initial_results_per_query": initial_results_per_query,
                "total_initial_results": sum(initial_results_per_query),
                "reranker": self.reranker.__class__.__name__
            })
            
            if latency_profiling:
                print(
                    f"get_all_ranked_results took {step_duration} seconds to run for {len(search_queries)} queries"
                )

            # --- RSE Step ---
            step_start_time = time.perf_counter()
            document_splits, document_start_points, unique_document_ids = get_meta_document(
                all_ranked_results=all_ranked_results,
                top_k_for_document_selection=top_k_for_document_selection,
            )

            # verify that we have a valid meta-document - otherwise return an empty list of segments
            if len(document_splits) == 0:
                query_logger.info("Query returned no results (empty meta-document)", extra=base_extra)
                return []

            # get the length of the meta-document so we don't have to pass in the whole list of splits
            meta_document_length = document_splits[-1]

            # get the relevance values for each chunk in the meta-document and use those to find the best segments
            all_relevance_values = get_relevance_values(
                all_ranked_results=all_ranked_results,
                meta_document_length=meta_document_length,
                document_start_points=document_start_points,
                unique_document_ids=unique_document_ids,
                irrelevant_chunk_penalty=irrelevant_chunk_penalty,
                decay_rate=decay_rate,
                chunk_length_adjustment=chunk_length_adjustment,
            )
            best_segments, scores = get_best_segments(
                all_relevance_values=all_relevance_values,
                document_splits=document_splits,
                max_length=max_length,
                overall_max_length=overall_max_length,
                minimum_value=minimum_value,
            )
            step_duration = time.perf_counter() - step_start_time
            
            # Log information about RSE step
            query_logger.debug("RSE complete", extra={
                **base_extra,
                "step": "rse", 
                "duration_s": round(step_duration, 4),
                "num_final_segments": len(best_segments),
                "segment_scores": [round(s, 4) for s in scores]
            })

            # --- Content Retrieval Step ---
            step_start_time = time.perf_counter()
            
            # convert the best segments into a list of dictionaries that contain the document id and the start and end of the chunk
            relevant_segment_info = []
            for segment_index, (start, end) in enumerate(best_segments):
                # find the document that this segment starts in
                for i, split in enumerate(document_splits):
                    if start < split:  # splits represent the end of each document
                        doc_start = document_splits[i - 1] if i > 0 else 0
                        relevant_segment_info.append(
                            {
                                "doc_id": unique_document_ids[i],
                                "chunk_start": start - doc_start,
                                "chunk_end": end - doc_start,
                            }
                        )  # NOTE: end index is non-inclusive
                        break

                score = scores[segment_index]
                relevant_segment_info[-1]["score"] = score

            # retrieve the content for each of the segments
            for segment_info in relevant_segment_info:
                segment_info["content"] = self._get_segment_content_from_database(
                    segment_info["doc_id"],
                    segment_info["chunk_start"],
                    segment_info["chunk_end"],
                    return_mode=return_mode,
                )
                start_page_number, end_page_number = self._get_segment_page_numbers(
                    segment_info["doc_id"],
                    segment_info["chunk_start"],
                    segment_info["chunk_end"]
                )
                segment_info["segment_page_start"] = start_page_number
                segment_info["segment_page_end"] = end_page_number

                # Deprecated keys, but needed for backwards compatibility
                segment_info["chunk_page_start"] = start_page_number
                segment_info["chunk_page_end"] = end_page_number

                # Backwards compatibility, where previously the content was stored in the "text" key
                if type(segment_info["content"]) == str:
                    segment_info["text"] = segment_info["content"]
                else:
                    segment_info["text"] = ""
            
            step_duration = time.perf_counter() - step_start_time
            
            # Log information about content retrieval step
            query_logger.debug("Content retrieval complete", extra={
                **base_extra, 
                "step": "content_retrieval", 
                "duration_s": round(step_duration, 4),
                "return_mode": return_mode
            })
            
            # Calculate and log overall query duration
            overall_duration = time.perf_counter() - overall_start_time
            query_logger.info("Query successful", extra={
                **base_extra, 
                "total_duration_s": round(overall_duration, 4), 
                "num_final_segments": len(relevant_segment_info)
            })

            return relevant_segment_info
            
        except Exception as e:
            # Log error with exception info
            overall_duration = time.perf_counter() - overall_start_time
            query_logger.error(
                "Query failed", 
                extra={
                    **base_extra,
                    "total_duration_s": round(overall_duration, 4),
                    "error": str(e)
                },
                exc_info=True
            )
            # Re-raise the exception
            raise