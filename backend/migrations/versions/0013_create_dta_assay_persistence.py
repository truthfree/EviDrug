"""Create normalized DTA assay query and evidence persistence."""

import sqlalchemy as sa
from alembic import op

revision = "0013_dta_assay_persistence"
down_revision = "0012_add_potency_criterion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dta_assay_queries",
        sa.Column("tool_call_id", sa.Uuid(), primary_key=True),
        sa.Column("request_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column(
            "analysis_id",
            sa.Uuid(),
            sa.ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("agent_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_version", sa.String(200), nullable=False),
        sa.Column("canonical_smiles_sha256", sa.String(64), nullable=False),
        sa.Column("uniprot_accession", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("external_requests", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'no_records') AND error_code IS NULL) OR "
            "(status = 'failed' AND error_code IS NOT NULL)",
            name="ck_dta_assay_query_outcome",
        ),
        sa.CheckConstraint("external_requests >= 0", name="ck_dta_assay_external_requests"),
        sa.CheckConstraint("duration_ms >= 0", name="ck_dta_assay_duration"),
        sa.CheckConstraint(
            "provider IN ('bindingdb', 'chembl', 'pubchem')",
            name="ck_dta_assay_provider",
        ),
    )
    op.create_index("ix_dta_assay_queries_analysis_id", "dta_assay_queries", ["analysis_id"])
    op.create_index("ix_dta_assay_queries_run_id", "dta_assay_queries", ["run_id"])
    op.create_table(
        "dta_assay_evidence",
        sa.Column(
            "query_tool_call_id",
            sa.Uuid(),
            sa.ForeignKey("dta_assay_queries.tool_call_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("source_record_id", sa.String(300), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("endpoint", sa.String(16), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(8), nullable=True),
        sa.Column("qualitative_outcome", sa.String(120), nullable=True),
        sa.Column("assay_description", sa.String(1000), nullable=True),
        sa.Column("doi", sa.String(200), nullable=True),
        sa.Column("pmid", sa.String(40), nullable=True),
        sa.UniqueConstraint(
            "query_tool_call_id",
            "source_record_id",
            name="uq_dta_assay_evidence_source_record",
        ),
        sa.CheckConstraint(
            "(kind = 'quantitative' AND endpoint IS NOT NULL AND value IS NOT NULL "
            "AND unit IS NOT NULL AND qualitative_outcome IS NULL) OR "
            "(kind = 'qualitative' AND endpoint IS NULL AND value IS NULL "
            "AND unit IS NULL AND qualitative_outcome IS NOT NULL)",
            name="ck_dta_assay_evidence_kind",
        ),
        sa.CheckConstraint("value IS NULL OR value > 0", name="ck_dta_assay_evidence_value"),
        sa.CheckConstraint("position >= 0", name="ck_dta_assay_evidence_position"),
        sa.CheckConstraint(
            "endpoint IS NULL OR endpoint IN ('Kd', 'Ki', 'IC50')",
            name="ck_dta_assay_evidence_endpoint",
        ),
        sa.CheckConstraint(
            "unit IS NULL OR unit IN ('pM', 'nM', 'uM', 'mM', 'M')",
            name="ck_dta_assay_evidence_unit",
        ),
    )


def downgrade() -> None:
    op.drop_table("dta_assay_evidence")
    op.drop_index("ix_dta_assay_queries_run_id", table_name="dta_assay_queries")
    op.drop_index("ix_dta_assay_queries_analysis_id", table_name="dta_assay_queries")
    op.drop_table("dta_assay_queries")
