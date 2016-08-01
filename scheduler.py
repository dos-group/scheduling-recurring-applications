from abc import ABCMeta, abstractmethod
from sklearn.cross_validation import LeaveOneOut
from cluster import Cluster
from application import Application
from complementarity import ComplementarityEstimation
from repeated_timer import RepeatedTimer
from threading import Lock
from typing import List
import time


class NoApplicationCanBeScheduled(BaseException):
    pass


class Scheduler(metaclass=ABCMeta):
    def __init__(self, estimation: ComplementarityEstimation, cluster: Cluster, update_interval=60):
        self.queue = []
        self.estimation = estimation
        self.cluster = cluster
        self._timer = RepeatedTimer(update_interval, self.update_estimation)
        self.scheduler_lock = Lock()
        self.started_at = None
        self.stopped_at = None

    def start(self):
        self.schedule()
        self._timer.start()
        self.started_at = time.time()

    def stop(self):
        self._timer.cancel()
        self.stopped_at = time.time()

    def update_estimation(self):
        for (apps, usage) in self.cluster.apps_usage():
            if len(apps) > 0:
                rate = self.usage2rate(usage)
                for rest, out in LeaveOneOut(len(apps)):
                    self.estimation.update_app(apps[out[0]], [apps[i] for i in rest], rate)

    @staticmethod
    def usage2rate(usage):
        return usage[0] + usage[1] + usage[2]

    def add(self, app: Application):
        self.queue.append(app)

    def add_all(self, apps: List[Application]):
        self.queue.extend(apps)

    def schedule(self):
        while len(self.queue) > 0:
            try:
                app = self.schedule_application()
            except NoApplicationCanBeScheduled:
                print("No Application can be scheduled right now")
                break
            app.start(self.cluster.resource_manager, self._on_app_finished)

    def _on_app_finished(self, app: Application):
        self.scheduler_lock.acquire()
        self.cluster.remove_applications(app)
        if len(self.queue) == 0 and len(self.cluster.applications()) == 0:
            self.stop()
            delta = self.stopped_at - self.started_at
            print("Queue took {:.0f}'{:.0f} to complete".format(delta // 60, delta % 60))
        else:
            self.schedule()
        self.scheduler_lock.release()

    @abstractmethod
    def schedule_application(self) -> Application:
        pass


class RoundRobin(Scheduler):
    def schedule_application(self):
        app = self.queue[0]
        self.place_quarter(app)
        return self.queue.pop(0)

    def place_containers(self, app: Application):
        if app.n_containers > self.cluster.available_containers():
            raise NoApplicationCanBeScheduled

        i = 0
        while i < app.n_containers:
            for node in self.cluster.nodes.values():
                if node.available_containers() > 0:
                    if i < app.n_tasks:
                        node.add_container(app.tasks[i])
                        i += 1
                    # add application master
                    elif i < app.n_containers:
                        node.add_container(app)
                        return

    def place_quarter(self, app: Application):
        if app.n_containers > self.cluster.available_containers():
            raise NoApplicationCanBeScheduled

        empty_nodes = list(filter(
            lambda n: n.available_containers() == n.n_containers,
            self.cluster.nodes.values()
        ))

        i = 0
        while len(empty_nodes) > 0 and i < app.n_containers:
            node = empty_nodes.pop()
            for j in range(4):
                k = i + j
                if k < app.n_tasks:
                    node.add_container(app.tasks[k])
                elif k < app.n_containers:
                    node.add_container(app)
                    return
            i += 4

        half_nodes = list(filter(
            lambda n: n.available_containers() == n.n_containers / 2,
            self.cluster.nodes.values()
        ))

        while len(half_nodes) > 0 and i < app.n_containers:
            node = half_nodes.pop()
            for j in range(4):
                k = i + j
                if k < app.n_tasks:
                    node.add_container(app.tasks[k])
                elif k < app.n_containers:
                    node.add_container(app)
                    return
            i += 4


class QueueOrder(RoundRobin):
    def __init__(self, jobs_to_peek=5, **kwargs):
        super().__init__(**kwargs)
        self.jobs_to_peek = jobs_to_peek

    def schedule_application(self):
        scheduled_apps = self.cluster.non_full_node_applications()
        available_containers = self.cluster.available_containers()
        index = list(range(min(self.jobs_to_peek, len(self.queue))))

        while len(index) > 0:
            best_i = self.estimation.best_app_index(
                scheduled_apps,
                [self.queue[i] for i in index]
            )
            if self.queue[best_i].n_containers <= available_containers:
                self.place_quarter(self.queue[best_i])
                return self.queue.pop(best_i)
            index.remove(best_i)
