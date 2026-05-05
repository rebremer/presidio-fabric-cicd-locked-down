# Fabric notebook source
# METADATA ********************
# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {
# META     "environment": {
# META       "environmentId": "00000000-0000-0000-0000-000000000003",
# META       "workspaceId": "00000000-0000-0000-0000-000000000000"
# META     }
# META   }
# META }

# MARKDOWN ********************

# # Presidio smoke test
#
# Verifies that the **Presidio** Fabric environment loads `presidio-analyzer`,
# `presidio-anonymizer` and the spaCy `en_core_web_sm` model, and that the
# analyzer + anonymizer pipeline returns the expected entities on a sample
# string.

# CELL ********************

# tldextract (transitive dep of presidio-analyzer's URL recognizer) tries
# to refresh the public-suffix list from publicsuffix.org on first use.
# DEP outbound blocks that, raising ConnectionError. Disable the network
# fetch on (a) the module-level singleton `tldextract.extract` (already
# constructed at import time) and (b) any new TLDExtract instance.
import tldextract as _tld
_snapshot_only = _tld.TLDExtract(suffix_list_urls=(), fallback_to_snapshot=True)
_tld.extract = _snapshot_only
_orig_init = _tld.TLDExtract.__init__
def _patched_init(self, *args, **kwargs):
    kwargs["suffix_list_urls"] = ()
    kwargs["fallback_to_snapshot"] = True
    _orig_init(self, *args, **kwargs)
_tld.TLDExtract.__init__ = _patched_init

import importlib.metadata as ilmd
import presidio_analyzer  # noqa: F401
import presidio_anonymizer  # noqa: F401
import spacy

print("presidio-analyzer  :", ilmd.version("presidio-analyzer"))
print("presidio-anonymizer:", ilmd.version("presidio-anonymizer"))
print("spacy              :", spacy.__version__)

# METADATA ********************
# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine

text = (
    "Hello, my name is John Doe. "
    "You can reach me at john.doe@contoso.com or +1 (415) 555-0132. "
    "My SSN is 123-45-6789."
)

# Default Presidio config asks for `en_core_web_lg`; we ship `_sm` instead.
nlp_engine = NlpEngineProvider(nlp_configuration={
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
}).create_engine()

analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
results = analyzer.analyze(text=text, language="en")

for r in results:
    print(f"{r.entity_type:15s} score={r.score:.2f}  '{text[r.start:r.end]}'")

# METADATA ********************
# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

anonymizer = AnonymizerEngine()
anonymized = anonymizer.anonymize(text=text, analyzer_results=results)
print(anonymized.text)

# METADATA ********************
# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

assert any(r.entity_type == "EMAIL_ADDRESS" for r in results), "EMAIL_ADDRESS not detected"
assert any(r.entity_type == "PHONE_NUMBER" for r in results), "PHONE_NUMBER not detected"
assert any(r.entity_type == "PERSON" for r in results), "PERSON not detected"
print("Presidio smoke test PASSED")

# METADATA ********************
# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
