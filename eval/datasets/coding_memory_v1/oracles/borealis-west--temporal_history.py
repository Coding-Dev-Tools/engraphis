"""Immutable oracle for borealis-west:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-borealis-west', service.current_policy()


if __name__ == "__main__":
    main()
