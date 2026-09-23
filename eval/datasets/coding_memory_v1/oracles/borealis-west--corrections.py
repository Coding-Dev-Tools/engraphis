"""Immutable oracle for borealis-west:corrections."""
import service


def main():
    assert service.current_timeout() == 74, service.current_timeout()


if __name__ == "__main__":
    main()
