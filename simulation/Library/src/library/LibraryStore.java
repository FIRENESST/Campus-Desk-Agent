package library;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.Clock;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

public final class LibraryStore {
    public static final int MIN_LOAN_DAYS = 1;
    public static final int MAX_LOAN_DAYS = 14;
    public static final int DEFAULT_LOAN_DAYS = 7;

    private final Map<String, Book> books = new LinkedHashMap<>();
    private final Map<String, Loan> loans = new LinkedHashMap<>();
    private final Path stateFile;
    private final Clock clock;
    private long nextLoanNumber = 1;

    public LibraryStore() {
        this(null, Clock.systemUTC());
    }

    public LibraryStore(Path stateFile) {
        this(stateFile, Clock.systemUTC());
    }

    LibraryStore(Path stateFile, Clock clock) {
        this(stateFile, clock, List.of());
    }

    LibraryStore(Path stateFile, Clock clock, List<LibraryCatalog.Title> titles) {
        this.stateFile = stateFile == null ? null : stateFile.toAbsolutePath().normalize();
        this.clock = clock;
        for (LibraryCatalog.Title title : titles) {
            books.put(title.id(), new Book(title.id(), title.title(), title.author(), title.category().id,
                    title.category().label, title.copies(), title.copies()));
        }
        loadLoans();
    }

    public synchronized List<Book> listBooks() {
        return List.copyOf(books.values());
    }

    public synchronized List<Loan> listLoans() {
        return List.copyOf(loans.values());
    }

    public synchronized List<CatalogBook> catalog() {
        Map<String, List<LoanWindow>> windows = new LinkedHashMap<>();
        for (Loan loan : loans.values()) {
            windows.computeIfAbsent(loan.bookId(), ignored -> new ArrayList<>()).add(loan.window());
        }
        return books.values().stream().map(book -> new CatalogBook(book,
                windows.getOrDefault(book.id(), List.of()).stream()
                        .sorted(Comparator.comparing(LoanWindow::dueAt)).toList())).toList();
    }

    public Loan borrow(String bookId, String borrowerName) {
        return borrow(bookId, borrowerName, DEFAULT_LOAN_DAYS);
    }

    public synchronized Loan borrow(String bookId, String borrowerName, int durationDays) {
        String normalizedBookId = requiredText(bookId, "book_id", 64);
        String normalizedBorrower = requiredText(borrowerName, "borrower_name", 100);
        validateDuration(durationDays);
        Book book = books.get(normalizedBookId);
        if (book == null) {
            throw new BusinessException("NOT_FOUND", "未找到图书: " + normalizedBookId);
        }
        for (Loan loan : loans.values()) {
            if (loan.bookId().equals(normalizedBookId) && loan.borrowerName().equals(normalizedBorrower)) {
                throw new BusinessException("CONFLICT", "同一读者不能重复借阅同一本书");
            }
        }
        if (book.availableCopies() == 0) {
            throw new BusinessException("CAPACITY_FULL", "该书暂无可借副本");
        }
        String loanId = "loan-" + String.format(Locale.ROOT, "%06d", nextLoanNumber);
        Loan loan = new Loan(loanId, book.id(), book.title(), normalizedBorrower,
                clock.instant().truncatedTo(ChronoUnit.MILLIS), durationDays);
        Map<String, Loan> updated = new LinkedHashMap<>(loans);
        updated.put(loanId, loan);
        saveLoans(updated, nextLoanNumber + 1);
        nextLoanNumber++;
        loans.put(loanId, loan);
        books.put(book.id(), book.withAvailableCopies(book.availableCopies() - 1));
        return loan;
    }

    public synchronized Loan returnLoan(String loanId) {
        String normalizedLoanId = requiredText(loanId, "loan_id", 64);
        Loan loan = loans.get(normalizedLoanId);
        if (loan == null) {
            throw new BusinessException("NOT_FOUND", "未找到借阅记录: " + normalizedLoanId);
        }
        Map<String, Loan> updated = new LinkedHashMap<>(loans);
        updated.remove(normalizedLoanId);
        saveLoans(updated, nextLoanNumber);
        loans.remove(normalizedLoanId);
        Book book = books.get(loan.bookId());
        books.put(book.id(), book.withAvailableCopies(book.availableCopies() + 1));
        return loan;
    }

    private static void validateDuration(int days) {
        if (days < MIN_LOAN_DAYS || days > MAX_LOAN_DAYS) {
            throw new BusinessException("INVALID_ARGUMENT", "借阅天数必须为 1 到 14 天");
        }
    }

    private void loadLoans() {
        if (stateFile == null || Files.notExists(stateFile)) {
            return;
        }
        try {
            Map<?, ?> saved = (Map<?, ?>) Json.parse(Files.readString(stateFile));
            nextLoanNumber = ((BigDecimal) saved.get("next_loan_number")).longValueExact();
            if (nextLoanNumber < 1) {
                throw new IllegalArgumentException("Invalid next loan number");
            }
            for (Object entry : (List<?>) saved.get("loans")) {
                Map<?, ?> values = (Map<?, ?>) entry;
                String id = (String) values.get("id");
                Book book = books.get(values.get("book_id"));
                String borrower = requiredText((String) values.get("borrower_name"), "borrower_name", 100);
                int days = ((BigDecimal) values.get("duration_days")).intValueExact();
                validateDuration(days);
                if (book == null || book.availableCopies() == 0 || !id.matches("loan-[0-9]+")
                        || Long.parseLong(id.substring(5)) >= nextLoanNumber || loans.containsKey(id)) {
                    throw new IllegalArgumentException("Invalid saved loan");
                }
                loans.put(id, new Loan(id, book.id(), book.title(), borrower,
                        Instant.parse((String) values.get("borrowed_at")), days));
                books.put(book.id(), book.withAvailableCopies(book.availableCopies() - 1));
            }
        } catch (IOException | RuntimeException exception) {
            throw new IllegalStateException("无法读取图书馆借阅存档", exception);
        }
    }

    private void saveLoans(Map<String, Loan> updated, long nextNumber) {
        if (stateFile == null) {
            return;
        }
        List<Map<String, Object>> saved = updated.values().stream().map(loan -> Map.<String, Object>of(
                "id", loan.id(), "book_id", loan.bookId(), "borrower_name", loan.borrowerName(),
                "borrowed_at", loan.borrowedAt().toString(), "duration_days", loan.durationDays())).toList();
        try {
            Path directory = Files.createDirectories(stateFile.getParent());
            Path temporary = Files.createTempFile(directory, "loans-", ".tmp");
            try {
                Files.writeString(temporary, Json.stringify(Map.of("next_loan_number", nextNumber, "loans", saved)));
                Files.move(temporary, stateFile, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            } finally {
                Files.deleteIfExists(temporary);
            }
        } catch (IOException exception) {
            throw new UncheckedIOException("无法保存图书馆借阅存档", exception);
        }
    }

    private static String requiredText(String value, String field, int maxLength) {
        if (value == null || value.isBlank()) {
            throw new BusinessException("INVALID_ARGUMENT", field + " 不能为空");
        }
        String normalized = value.trim();
        if (normalized.length() > maxLength) {
            throw new BusinessException("INVALID_ARGUMENT", field + " 长度不能超过 " + maxLength);
        }
        return normalized;
    }

    public record Book(String id, String title, String author, String categoryId, String category,
                       int totalCopies, int availableCopies) {
        private Book withAvailableCopies(int availableCopies) {
            return new Book(id, title, author, categoryId, category, totalCopies, availableCopies);
        }
    }

    public record CatalogBook(Book book, List<LoanWindow> loans) {
    }

    public record LoanWindow(Instant borrowedAt, Instant dueAt, int durationDays) {
    }

    public record Loan(String id, String bookId, String bookTitle, String borrowerName, Instant borrowedAt, int durationDays) {
        public LoanWindow window() {
            return new LoanWindow(borrowedAt, borrowedAt.plus(durationDays, ChronoUnit.DAYS), durationDays);
        }
    }

    public static final class BusinessException extends RuntimeException {
        private final String code;

        private BusinessException(String code, String message) {
            super(message);
            this.code = code;
        }

        public String code() {
            return code;
        }

        public boolean retryable() {
            return false;
        }
    }
}
