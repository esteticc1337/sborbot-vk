class Event:
    def __init__(self, start_time, description):
        self.end_time = None
        self.start_time = start_time
        self.description = description

    def set_end_time(self, time):
        self.end_time = time

    def pretty_print(self):
        return f'{self.start_time} - {self.end_time} - {self.description}'


class Day:
    events = []

    def __init__(self):
        pass

    def add_event(self, event):
        self.events.append(event)

    def set_events(self, events):
        self.events = events


class Schedule:
    days = [Day(), Day(), Day()]
    current_day = 0

    def __init__(self):
        pass

    def set_current_day(self, current_day):
        if int(current_day) == 1:
            self.current_day = self.days[0]
        elif int(current_day) == 2:
            self.current_day = self.days[1]
        elif int(current_day) == 3:
            self.current_day = self.days[2]
        elif int(current_day) == 4:
            self.current_day = "Закончися"
        else:
            self.current_day = "Не начался"
