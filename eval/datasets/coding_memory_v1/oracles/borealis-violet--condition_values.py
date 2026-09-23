"""Immutable oracle for borealis-violet:condition_values."""
import service


def main():
    assert service.retry_budget() == {'limit': 58, 'unit': 'events/hour', 'enabled': True}, service.retry_budget()


if __name__ == "__main__":
    main()
