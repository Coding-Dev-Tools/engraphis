"""Immutable oracle for grove-green:condition_values."""
import service


def main():
    assert service.retry_budget() == {'limit': 82, 'unit': 'requests/minute', 'enabled': True}, service.retry_budget()


if __name__ == "__main__":
    main()
