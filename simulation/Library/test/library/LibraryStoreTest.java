package library;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

public final class LibraryStoreTest {
    public static void main(String[] args) throws Exception {
        borrowAndReturnUpdateSharedCopiesAndLoans();
        rejectsDuplicateUnknownIdsAndInvalidArguments();
        persistsLoanWindowsAndReturnsBeforeOrAfterDueDate();
        concurrentBorrowersNeverExceedCapacity();
        concurrentDuplicateBorrowHasOneSuccessAndConflicts();
        System.out.println("LibraryStoreTest: PASS");
    }

    private static void borrowAndReturnUpdateSharedCopiesAndLoans() {
        LibraryStore store = Fixtures.store();
        LibraryStore.Book before = book(store, "b-001");
        LibraryStore.Loan loan = store.borrow(" b-001 ", "  张三  ");
        if (!"张三".equals(loan.borrowerName()) || !"b-001".equals(loan.bookId())) {
            throw new AssertionError("借阅输入应去除首尾空白");
        }
        if (store.listLoans().size() != 1 || book(store, "b-001").availableCopies() != before.totalCopies() - 1) {
            throw new AssertionError("借阅后借阅记录或可借副本未更新");
        }
        LibraryStore.Loan returned = store.returnLoan(loan.id());
        if (!returned.id().equals(loan.id()) || !store.listLoans().isEmpty()
                || book(store, "b-001").availableCopies() != before.totalCopies()) {
            throw new AssertionError("归还后记录或库存未恢复");
        }
    }

    private static void rejectsDuplicateUnknownIdsAndInvalidArguments() {
        LibraryStore store = Fixtures.store();
        LibraryStore.Loan loan = store.borrow("b-001", "张三");
        expectCode("CONFLICT", () -> store.borrow("b-001", "张三"));
        expectCode("NOT_FOUND", () -> store.borrow("missing", "李四"));
        expectCode("NOT_FOUND", () -> store.returnLoan("missing"));
        expectCode("INVALID_ARGUMENT", () -> store.borrow("", "李四"));
        expectCode("INVALID_ARGUMENT", () -> store.borrow("b-001", " "));
        expectCode("INVALID_ARGUMENT", () -> store.returnLoan(null));
        expectCode("INVALID_ARGUMENT", () -> store.borrow("b-002", "李四", 0));
        expectCode("INVALID_ARGUMENT", () -> store.borrow("b-002", "李四", 15));
        if (store.listLoans().size() != 1) {
            throw new AssertionError("失败写入不得产生部分记录");
        }
    }

    private static void persistsLoanWindowsAndReturnsBeforeOrAfterDueDate() throws Exception {
        Path directory = Files.createTempDirectory("library-test-");
        Path state = directory.resolve("loans.json");
        Instant now = Instant.parse("2026-11-12T08:00:00Z");
        Clock clock = Clock.fixed(now, ZoneOffset.UTC);
        try {
            LibraryStore store = Fixtures.store(state, clock);
            LibraryStore.Loan shortLoan = store.borrow("b-001", "短借", 1);
            LibraryStore.Loan longLoan = store.borrow("b-001", "长借", 14);
            assert shortLoan.window().dueAt().equals(now.plus(1, ChronoUnit.DAYS));
            assert longLoan.window().dueAt().equals(now.plus(14, ChronoUnit.DAYS));

            LibraryStore reopened = Fixtures.store(state, clock);
            assert reopened.catalog().getFirst().loans().equals(List.of(shortLoan.window(), longLoan.window()));
            assert book(reopened, "b-001").availableCopies() == 0;
            reopened.returnLoan(shortLoan.id());
            assert book(reopened, "b-001").availableCopies() == 1;
            assert reopened.catalog().getFirst().loans().equals(List.of(longLoan.window()));

            Clock afterDueDate = Clock.fixed(now.plus(15, ChronoUnit.DAYS), ZoneOffset.UTC);
            LibraryStore overdue = Fixtures.store(state, afterDueDate);
            assert book(overdue, "b-001").availableCopies() == 1;
            overdue.returnLoan(longLoan.id());
            assert book(overdue, "b-001").availableCopies() == 2;
            LibraryStore.Loan next = overdue.borrow("b-001", "新读者");
            assert next.durationDays() == LibraryStore.DEFAULT_LOAN_DAYS;
            assert !next.id().equals(shortLoan.id()) && !next.id().equals(longLoan.id());
        } finally {
            Files.deleteIfExists(state);
            Files.deleteIfExists(directory);
        }
    }

    private static void concurrentBorrowersNeverExceedCapacity() throws Exception {
        LibraryStore store = Fixtures.store();
        int copies = book(store, "b-003").totalCopies();
        List<String> outcomes = runConcurrently(16, index -> {
            try {
                store.borrow("b-003", "读者-" + index);
                return "OK";
            } catch (LibraryStore.BusinessException exception) {
                return exception.code();
            }
        });
        long successes = outcomes.stream().filter("OK"::equals).count();
        long full = outcomes.stream().filter("CAPACITY_FULL"::equals).count();
        if (successes != copies || full != outcomes.size() - copies
                || book(store, "b-003").availableCopies() != 0
                || store.listLoans().size() != copies) {
            throw new AssertionError("并发借阅超出容量或错误语义不稳定: " + outcomes);
        }
    }

    private static void concurrentDuplicateBorrowHasOneSuccessAndConflicts() throws Exception {
        LibraryStore store = Fixtures.store();
        List<String> outcomes = runConcurrently(12, index -> {
            try {
                store.borrow("b-003", "同一读者");
                return "OK";
            } catch (LibraryStore.BusinessException exception) {
                return exception.code();
            }
        });
        long successes = outcomes.stream().filter("OK"::equals).count();
        long conflicts = outcomes.stream().filter("CONFLICT"::equals).count();
        if (successes != 1 || conflicts != outcomes.size() - 1) {
            throw new AssertionError("并发重复借阅必须仅一次成功，其余为 CONFLICT: " + outcomes);
        }
    }

    private static List<String> runConcurrently(int count, BorrowAction action) throws Exception {
        ExecutorService executor = Executors.newFixedThreadPool(count);
        try {
            List<Callable<String>> tasks = new ArrayList<>();
            for (int index = 0; index < count; index++) {
                int borrowerIndex = index;
                tasks.add(() -> action.run(borrowerIndex));
            }
            List<String> outcomes = new ArrayList<>();
            for (Future<String> future : executor.invokeAll(tasks)) {
                outcomes.add(future.get());
            }
            return outcomes;
        } finally {
            executor.close();
        }
    }

    private static LibraryStore.Book book(LibraryStore store, String id) {
        return store.listBooks().stream().filter(book -> book.id().equals(id)).findFirst()
                .orElseThrow(() -> new AssertionError("缺少测试图书 " + id));
    }

    private static void expectCode(String code, Runnable action) {
        try {
            action.run();
            throw new AssertionError("应失败并返回 " + code);
        } catch (LibraryStore.BusinessException exception) {
            if (!code.equals(exception.code()) || exception.retryable()) {
                throw new AssertionError("错误码或 retryable 不正确: " + exception.code());
            }
        }
    }

    @FunctionalInterface
    private interface BorrowAction {
        String run(int index) throws Exception;
    }
}
