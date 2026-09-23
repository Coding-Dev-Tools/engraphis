"""Immutable oracle for delta-violet:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-delta-violet', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
