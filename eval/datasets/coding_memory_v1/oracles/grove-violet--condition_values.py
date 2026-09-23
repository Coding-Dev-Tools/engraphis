"""Immutable oracle for grove-violet:condition_values."""
import service


def main():
    assert service.retry_budget() == {'limit': 83, 'unit': 'requests/minute', 'enabled': True}, service.retry_budget()


if __name__ == "__main__":
    main()
