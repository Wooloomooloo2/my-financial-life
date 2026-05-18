# ===========================================================================
# app/core/ontology/namespaces.py
#
# IRI constants for all ontology namespaces used by My Financial Life.
# Import from here rather than hardcoding IRI strings anywhere else.
#
# Both MRL and MFL namespaces are defined here since MFL reuses MRL classes
# directly (mrl:Account, mrl:Person, mrl:Currency etc.)
# ===========================================================================

from pyoxigraph import NamedNode


# ---------------------------------------------------------------------------
# Namespace base URIs
# ---------------------------------------------------------------------------

MRL  = "https://myretirementlife.app/ontology#"
MRLX = "https://myretirementlife.app/ontology/ext#"
MFL  = "https://myfinanciallife.app/ontology#"
MFLX = "https://myfinanciallife.app/ontology/ext#"

# Standard vocabularies
RDF  = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL  = "http://www.w3.org/2002/07/owl#"
XSD  = "http://www.w3.org/2001/XMLSchema#"
SKOS = "http://www.w3.org/2004/02/skos/core#"


# ---------------------------------------------------------------------------
# Named graph IRIs
# ---------------------------------------------------------------------------

ONTOLOGY_GRAPH = NamedNode("https://myfinanciallife.app/ontology/graph")
DATA_GRAPH     = NamedNode("https://myfinanciallife.app/data/graph")


# ---------------------------------------------------------------------------
# Helper — build a NamedNode from a namespace + local name
# ---------------------------------------------------------------------------

def mrl(local: str)  -> NamedNode: return NamedNode(MRL  + local)
def mrlx(local: str) -> NamedNode: return NamedNode(MRLX + local)
def mfl(local: str)  -> NamedNode: return NamedNode(MFL  + local)
def mflx(local: str) -> NamedNode: return NamedNode(MFLX + local)
def xsd(local: str)  -> NamedNode: return NamedNode(XSD  + local)
def skos(local: str) -> NamedNode: return NamedNode(SKOS + local)
def rdf(local: str)  -> NamedNode: return NamedNode(RDF  + local)


# ---------------------------------------------------------------------------
# Commonly used RDF/OWL/SKOS nodes
# ---------------------------------------------------------------------------

RDF_TYPE        = rdf("type")
RDFS_LABEL      = NamedNode(RDFS + "label")
RDFS_SUBCLASS   = NamedNode(RDFS + "subClassOf")
SKOS_PREF_LABEL = skos("prefLabel")
SKOS_IN_SCHEME  = skos("inScheme")
SKOS_BROADER    = skos("broader")
SKOS_NOTATION   = skos("notation")


# ---------------------------------------------------------------------------
# MRL class nodes — reused directly in MFL
# ---------------------------------------------------------------------------

MRL_PERSON             = mrl("Person")
MRL_ACCOUNT            = mrl("Account")
MRL_CASH_ACCOUNT       = mrl("CashAccount")
MRL_INVESTMENT_ACCOUNT = mrl("InvestmentAccount")
MRL_CREDIT_CARD        = mrl("CreditCardAccount")
MRL_PROPERTY_ASSET     = mrl("PropertyAsset")
MRL_CURRENCY           = mrl("Currency")
MRL_JURISDICTION       = mrl("Jurisdiction")


# ---------------------------------------------------------------------------
# MRL property nodes — reused directly in MFL
# ---------------------------------------------------------------------------

MRL_ACCOUNT_NAME     = mrl("accountName")
MRL_ACCOUNT_BALANCE  = mrl("accountBalance")
MRL_ACCOUNT_CURRENCY = mrl("accountCurrency")
MRL_ACCOUNT_TYPE     = mrl("accountType")
MRL_ACCOUNT_NOTES    = mrl("accountNotes")
MRL_BALANCE_DATE     = mrl("balanceDate")
MRL_IS_LIABILITY     = mrl("isLiability")
MRL_OWNED_BY         = mrl("ownedBy")
MRL_EXCHANGE_RATE    = mrl("exchangeRateToBase")
MRL_CURRENCY_CODE    = mrl("currencyCode")
MRL_CURRENCY_SYMBOL  = mrl("currencySymbol")
MRL_FIRST_NAME       = mrl("firstName")
MRL_LAST_NAME        = mrl("lastName")
MRL_BASE_CURRENCY    = mrl("baseCurrency")


# ---------------------------------------------------------------------------
# MFL class nodes
# ---------------------------------------------------------------------------

MFL_TRANSACTION      = mfl("Transaction")
MFL_PAYEE            = mfl("Payee")
MFL_CATEGORY_RULE    = mfl("CategoryRule")
MFL_IMPORT_BATCH     = mfl("ImportBatch")
MFL_VALUATION_EVENT  = mfl("ValuationEvent")
MFL_APP_SETTINGS     = mfl("AppSettings")


# ---------------------------------------------------------------------------
# MFL property nodes — Transaction
# ---------------------------------------------------------------------------

MFL_TRANSACTION_DATE   = mfl("transactionDate")
MFL_POST_DATE          = mfl("postDate")
MFL_AMOUNT             = mfl("amount")
MFL_TRANSACTION_TYPE   = mfl("transactionType")
MFL_TRANSACTION_STATUS = mfl("transactionStatus")
MFL_ON_ACCOUNT         = mfl("onAccount")
MFL_PAYEE_PROP         = mfl("payee")
MFL_PAYEE_RAW          = mfl("payeeRaw")
MFL_CATEGORY           = mfl("category")
MFL_MEMO               = mfl("memo")
MFL_NOTES              = mfl("notes")
MFL_REFERENCE          = mfl("reference")
MFL_IMPORT_HASH        = mfl("importHash")
MFL_IMPORT_BATCH_PROP  = mfl("importBatch")
MFL_TRANSFER_COUNTERPART = mfl("transferCounterpart")
MFL_IS_MANUAL_ENTRY    = mfl("isManualEntry")


# ---------------------------------------------------------------------------
# MFL property nodes — Payee
# ---------------------------------------------------------------------------

MFL_PAYEE_NAME        = mfl("payeeName")
MFL_PAYEE_NOTES       = mfl("payeeNotes")
MFL_DEFAULT_CATEGORY  = mfl("defaultCategory")


# ---------------------------------------------------------------------------
# MFL property nodes — ImportBatch
# ---------------------------------------------------------------------------

MFL_IMPORT_DATE              = mfl("importDate")
MFL_IMPORT_FORMAT            = mfl("importFormat")
MFL_IMPORT_FILE_NAME         = mfl("importFileName")
MFL_IMPORT_TARGET_ACCOUNT    = mfl("importTargetAccount")
MFL_IMPORT_TRANSACTION_COUNT = mfl("importTransactionCount")
MFL_IMPORT_NEW_COUNT         = mfl("importNewCount")
MFL_IMPORT_DUPLICATE_COUNT   = mfl("importDuplicateCount")
MFL_IMPORT_COLUMN_MAP        = mfl("importColumnMap")


# ---------------------------------------------------------------------------
# MFL property nodes — ValuationEvent
# ---------------------------------------------------------------------------

MFL_VALUATION_DATE        = mfl("valuationDate")
MFL_VALUATION_AMOUNT      = mfl("valuationAmount")
MFL_VALUATION_SOURCE      = mfl("valuationSource")
MFL_VALUATION_NOTES       = mfl("valuationNotes")
MFL_VALUATION_FOR_ACCOUNT = mfl("valuationForAccount")


# ---------------------------------------------------------------------------
# MFL property nodes — AppSettings
# ---------------------------------------------------------------------------

MFL_SETTINGS_OWNER     = mfl("settingsOwner")
MFL_DEFAULT_TIMESCALE  = mfl("defaultTimescale")
MFL_DEFAULT_ACCOUNTS   = mfl("defaultAccounts")


# ---------------------------------------------------------------------------
# MFLX controlled vocabulary nodes — frequently used
# ---------------------------------------------------------------------------

MFLX_STATUS_PENDING    = mflx("TransactionStatus_Pending")
MFLX_STATUS_UNCLEARED  = mflx("TransactionStatus_Uncleared")
MFLX_STATUS_CLEARED    = mflx("TransactionStatus_Cleared")
MFLX_STATUS_RECONCILED = mflx("TransactionStatus_Reconciled")

MFLX_TYPE_DEBIT    = mflx("TransactionType_Debit")
MFLX_TYPE_CREDIT   = mflx("TransactionType_Credit")
MFLX_TYPE_TRANSFER = mflx("TransactionType_Transfer")

MFLX_CAT_UNCATEGORISED = mflx("TransactionCategory_Uncategorised")


# ---------------------------------------------------------------------------
# XSD datatype nodes — used when writing literals
# ---------------------------------------------------------------------------

XSD_STRING   = xsd("string")
XSD_DECIMAL  = xsd("decimal")
XSD_INTEGER  = xsd("integer")
XSD_BOOLEAN  = xsd("boolean")
XSD_DATE     = xsd("date")
XSD_DATETIME = xsd("dateTime")