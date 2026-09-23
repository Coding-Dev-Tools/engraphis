"""Immutable oracle for borealis-green:corrections."""
import service


def main():
    assert service.current_timeout() == 78, service.current_timeout()


if __name__ == "__main__":
    main()
