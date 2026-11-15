package cn.campusdesk.calendar;

import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;
import android.os.Handler;
import android.os.Looper;

import java.util.concurrent.TimeUnit;

public final class CalendarJob extends JobService {
    private static final int JOB_ID = 1;
    private final Handler main = new Handler(Looper.getMainLooper());
    private Run current;

    private static final class Run {
        volatile boolean canceled;
        final JobParameters parameters;

        Run(JobParameters parameters) {
            this.parameters = parameters;
        }
    }

    static void schedule(Context context) {
        JobScheduler scheduler = context.getSystemService(JobScheduler.class);
        if (scheduler.getPendingJob(JOB_ID) == null) {
            scheduler.schedule(new JobInfo.Builder(JOB_ID, new ComponentName(context, CalendarJob.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY).setPersisted(true)
                .setPeriodic(TimeUnit.MINUTES.toMillis(30)).build());
        }
    }

    static void cancel(Context context) {
        context.getSystemService(JobScheduler.class).cancel(JOB_ID);
    }

    @Override public boolean onStartJob(JobParameters parameters) {
        if (!CalendarSync.connected(this)) return false;
        if (current != null) current.canceled = true;
        Run run = new Run(parameters);
        current = run;
        new Thread(() -> {
            try {
                CalendarSync.refresh(getApplicationContext(), null, () -> run.canceled);
            } catch (CalendarSync.Failure ignored) {
                // A failure is shown in the app. The periodic job will try again at its next interval.
            } finally {
                main.post(() -> {
                    if (current == run) {
                        current = null;
                        jobFinished(run.parameters, false);
                    }
                });
            }
        }, "campus-calendar-job").start();
        return true;
    }

    @Override public boolean onStopJob(JobParameters parameters) {
        if (current != null && current.parameters.getJobId() == parameters.getJobId()) {
            current.canceled = true;
            current = null;
        }
        return CalendarSync.connected(this);
    }
}
