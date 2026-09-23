"""Immutable oracle for island-violet:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-island-violet', service.current_policy()


if __name__ == "__main__":
    main()
