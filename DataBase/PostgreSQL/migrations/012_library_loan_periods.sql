ALTER TABLE campus_minta_operations
    ADD COLUMN loan_days SMALLINT CHECK (loan_days BETWEEN 1 AND 14),
    ADD COLUMN loan_borrowed_at TIMESTAMPTZ,
    ADD CONSTRAINT campus_minta_loan_period CHECK (
        (service = 'library' OR (loan_days IS NULL AND loan_borrowed_at IS NULL))
        AND (loan_borrowed_at IS NULL OR loan_days IS NOT NULL)
    );
