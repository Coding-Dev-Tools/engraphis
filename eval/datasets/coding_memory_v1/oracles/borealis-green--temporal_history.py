"""Immutable oracle for borealis-green:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-borealis-green', service.current_policy()


if __name__ == "__main__":
    main()
